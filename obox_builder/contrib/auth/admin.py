"""Superuser-only screens for managing auth users and role permissions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, cast

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import selectinload
from starlette.datastructures import FormData

from obox_builder.auth import RBAC, Identity
from obox_builder.builder.http import OboxRoute, read_form
from obox_builder.builder.ui import build_navigation
from obox_builder.contrib.auth.models import Role, User
from obox_builder.contrib.auth.passwords import hash_password
from obox_builder.errors import (
    N_,
    BuilderValidationError,
    NotAuthenticated,
    NotFound,
    PermissionDenied,
)
from obox_builder.i18n import context as translation_context
from obox_builder.i18n import translate

if TYPE_CHECKING:
    from obox_builder.application import Obox
    from obox_builder.context import Context
    from obox_builder.contrib.auth import Auth

ACTIONS = ("list", "retrieve", "create", "update", "delete")
MAX_LENGTH = 150
EMAIL_MAX_LENGTH = 254


def user_model(auth: Auth) -> type[Any]:
    return cast(type[Any], getattr(auth.backend, "user_model", User))


def role_model(auth: Auth) -> type[Any]:
    relation = sa.inspect(user_model(auth)).relationships.get("roles")
    return cast(type[Any], relation.mapper.class_ if relation is not None else Role)


def permission_tree(app: Obox) -> tuple[list[dict[str, Any]], set[str]]:
    resources: dict[str, dict[str, set[str]]] = {}
    for registration in app._registrations.values():
        builder = registration.builder
        if isinstance(builder.policy, RBAC):
            service, resource = builder.policy.resource.split(".", 1)
        else:
            if builder.slug is None:
                continue
            service, resource = "admin", builder.slug
        resources.setdefault(service, {}).setdefault(resource, set()).update(ACTIONS)
    for resource in ("users", "roles"):
        resources.setdefault("auth", {}).setdefault(resource, set()).update(ACTIONS)
    tree: list[dict[str, Any]] = []
    known: set[str] = set()
    for service in sorted(resources):
        items = []
        for resource in sorted(resources[service]):
            actions = []
            for action in ACTIONS:
                value = f"{service}.{resource}.{action}"
                known.add(value)
                actions.append({"action": action, "value": value})
            items.append({"name": resource, "actions": actions})
        tree.append({"name": service, "resources": items})
    return tree, known


def preview_permissions(
    tree: list[dict[str, Any]], roles: Sequence[Any], selected: set[int]
) -> tuple[list[dict[str, Any]], bool]:
    granted = {role.id: set(role.permissions) for role in roles}
    preview: list[dict[str, Any]] = []
    for service in tree:
        resources = []
        for resource in service["resources"]:
            actions = []
            for action in resource["actions"]:
                role_ids = [
                    role_id for role_id in sorted(granted) if action["value"] in granted[role_id]
                ]
                actions.append(
                    {
                        **action,
                        "role_ids": role_ids,
                        "selected": bool(set(role_ids) & selected),
                    }
                )
            resources.append(
                {
                    **resource,
                    "actions": actions,
                    "has_permissions": any(action["selected"] for action in actions),
                }
            )
        preview.append(
            {
                **service,
                "resources": resources,
                "has_permissions": any(resource["has_permissions"] for resource in resources),
            }
        )
    return preview, any(service["has_permissions"] for service in preview)


def role_form(
    role: Any | None,
    *,
    values: Mapping[str, Any] | None = None,
    errors: Mapping[str, list[str]] | None = None,
    open: bool = False,
) -> dict[str, Any]:
    return {
        "key": str(role.id) if role is not None else "new",
        "id": role.id if role is not None else None,
        "name": values["name"] if values is not None else (role.name if role else ""),
        "permissions": set(
            values["permissions"]
            if values is not None
            else (role.permissions if role is not None else ())
        ),
        "errors": dict(errors or {}),
        "open": open,
    }


def user_values(user: Any | None) -> tuple[dict[str, str], set[int]]:
    if user is None:
        return (
            {
                "username": "",
                "email": "",
                "first_name": "",
                "last_name": "",
                "department": "",
                "is_active": "1",
                "password": "",
                "confirm_password": "",
            },
            set(),
        )
    return (
        {
            "username": user.username,
            "email": user.email,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "department": user.department,
            "is_active": "1" if user.is_active else "",
            "password": "",
            "confirm_password": "",
        },
        {role.id for role in user.roles},
    )


def admin_error(request: Request, code: str) -> str:
    messages = {
        "self": N_("You cannot delete your own account."),
        "last_superuser": N_("At least one superuser is required."),
    }
    message = messages.get(code)
    return translate(request, message) if message is not None else ""


def router(app: Obox, auth: Auth) -> APIRouter:
    routes = APIRouter(prefix=auth.prefix, route_class=OboxRoute)

    async def superuser(request: Request) -> Identity:
        principal = await auth.principal(request)
        if not principal.is_authenticated:
            raise NotAuthenticated(N_("Authentication is required."))
        if not getattr(principal, "is_superuser", False):
            raise PermissionDenied(N_("Superuser access is required."))
        return principal

    async def render(
        request: Request,
        ctx: Context,
        template: str,
        *,
        title: str,
        active: str,
        status: int = 200,
        **context: Any,
    ) -> HTMLResponse:
        return cast(
            HTMLResponse,
            app.templates.TemplateResponse(
                request=request,
                name=template,
                context={
                    **translation_context(request),
                    **context,
                    "title": title,
                    "site_title": app.app.title,
                    "base_template": app.settings.base_template,
                    "navigation": await build_navigation(app, request, ctx, active_admin=active),
                    "principal": ctx.principal,
                    "auth": auth,
                    "csrf": app.csrf.token(request, ctx.principal),
                    "users_url": auth.users_url(request),
                    "roles_url": auth.roles_url(request),
                },
                status_code=status,
                headers={"cache-control": "no-store"},
            ),
        )

    async def form_data(
        request: Request,
        principal: Identity,
        *,
        multiple: frozenset[str] = frozenset(),
    ) -> FormData:
        form = await read_form(request)
        app.csrf.validate(request, principal, str(form.get("csrf_token", "")))
        for key in form:
            if key not in multiple and len(form.getlist(key)) != 1:
                raise BuilderValidationError(
                    {"__all__": [N_("Duplicate form fields are not permitted.")]}
                )
        return form

    async def load_user(ctx: Context, id: int) -> Any:
        model = user_model(auth)
        statement = sa.select(model).where(model.id == id)
        if "roles" in sa.inspect(model).relationships:
            statement = statement.options(selectinload(model.roles))
        user = (await ctx.session.scalars(statement)).unique().one_or_none()
        if user is None:
            raise NotFound(N_("User not found."))
        return user

    async def all_roles(ctx: Context) -> list[Any]:
        model = role_model(auth)
        statement = sa.select(model).order_by(model.id)
        return list((await ctx.session.scalars(statement)).unique())

    async def user_list(request: Request, principal: Identity = Depends(superuser)) -> HTMLResponse:
        async with app.uow(principal=principal, request=request, source="ui") as ctx:
            model = user_model(auth)
            users = list((await ctx.session.scalars(sa.select(model).order_by(model.id))).unique())
            return await render(
                request,
                ctx,
                "obox/auth/users.html",
                title=translate(request, "Users"),
                active="users",
                users=users,
                saved=request.query_params.get("saved") == "1",
                deleted=request.query_params.get("deleted") == "1",
                error=admin_error(request, request.query_params.get("error", "")),
            )

    async def user_form_page(
        request: Request,
        principal: Identity,
        id: int | None = None,
    ) -> HTMLResponse | RedirectResponse:
        async with app.uow(principal=principal, request=request, source="ui") as ctx:
            model = user_model(auth)
            user = await load_user(ctx, id) if id is not None else None
            roles = await all_roles(ctx)
            if request.method == "GET":
                values, selected = user_values(user)
                preview, preview_has = preview_permissions(permission_tree(app)[0], roles, selected)
                return await render(
                    request,
                    ctx,
                    "obox/auth/user_form.html",
                    title=translate(request, "Edit user" if user else "New user"),
                    active="users",
                    user=user,
                    values=values,
                    selected_roles=selected,
                    roles=roles,
                    permission_preview=preview,
                    preview_has_permissions=preview_has,
                    errors={},
                )

            form = await form_data(request, principal, multiple=frozenset({"role_ids"}))
            errors: dict[str, list[str]] = {}
            username = str(form.get("username", "")).strip()
            email = str(form.get("email", "")).strip()
            first_name = str(form.get("first_name", "")).strip()
            last_name = str(form.get("last_name", "")).strip()
            department = str(form.get("department", "")).strip()
            password = str(form.get("password", ""))
            confirmation = str(form.get("confirm_password", ""))
            is_active = form.get("is_active") == "1"

            if not username:
                errors["username"] = [N_("This field is required.")]
            elif len(username) > MAX_LENGTH:
                errors["username"] = [N_("Use at most 150 characters.")]
            else:
                current = user.id if user is not None else -1
                exists = await ctx.session.scalar(
                    sa.select(model.id).where(model.username == username, model.id != current)
                )
                if exists is not None:
                    errors["username"] = [N_("This username is already taken.")]
            if len(email) > EMAIL_MAX_LENGTH:
                errors["email"] = [N_("Use at most 254 characters.")]
            if len(first_name) > MAX_LENGTH:
                errors["first_name"] = [N_("Use at most 150 characters.")]
            if len(last_name) > MAX_LENGTH:
                errors["last_name"] = [N_("Use at most 150 characters.")]
            if len(department) > MAX_LENGTH:
                errors["department"] = [N_("Use at most 150 characters.")]

            requested: set[int] = set()
            role_error = False
            for value in form.getlist("role_ids"):
                try:
                    requested.add(int(str(value)))
                except (TypeError, ValueError):
                    role_error = True
            roles_by_id = {role.id: role for role in roles}
            if role_error or requested - roles_by_id.keys():
                errors["role_ids"] = [N_("Choose valid roles.")]
            selected = requested & roles_by_id.keys()

            encoded: str | None = None
            if not password and user is None:
                errors["password"] = [N_("This field is required.")]
            elif password != confirmation:
                errors["confirm_password"] = [N_("Passwords do not match.")]
            if password:
                try:
                    encoded = await hash_password(password)
                except BuilderValidationError as error:
                    errors.setdefault("password", []).extend(error.errors.get("password", []))

            if user is not None and user.is_active and not is_active:
                if user.id == principal.id:
                    errors["is_active"] = [N_("You cannot deactivate your own account.")]
                elif user.is_superuser:
                    active_superusers = await ctx.session.scalar(
                        sa.select(sa.func.count())
                        .select_from(model)
                        .where(model.is_superuser.is_(True), model.is_active.is_(True))
                    )
                    if (active_superusers or 0) <= 1:
                        errors["is_active"] = [N_("At least one superuser must remain active.")]

            if errors:
                values = {
                    "username": username,
                    "email": email,
                    "first_name": first_name,
                    "last_name": last_name,
                    "department": department,
                    "is_active": "1" if is_active else "",
                    "password": "",
                    "confirm_password": "",
                }
                preview, preview_has = preview_permissions(permission_tree(app)[0], roles, selected)
                return await render(
                    request,
                    ctx,
                    "obox/auth/user_form.html",
                    title=translate(request, "Edit user" if user else "New user"),
                    active="users",
                    status=422,
                    user=user,
                    values=values,
                    selected_roles=selected,
                    roles=roles,
                    permission_preview=preview,
                    preview_has_permissions=preview_has,
                    errors=errors,
                )

            if user is None:
                user = model(
                    username=username,
                    email=email,
                    password_hash=encoded,
                    first_name=first_name,
                    last_name=last_name,
                    department=department,
                    is_active=is_active,
                )
                ctx.session.add(user)
            else:
                user.username = username
                user.email = email
                user.first_name = first_name
                user.last_name = last_name
                user.department = department
                user.is_active = is_active
                if encoded is not None:
                    user.password_hash = encoded
            user.roles = [roles_by_id[role_id] for role_id in sorted(selected)]
            return RedirectResponse(f"{auth.users_url(request)}?saved=1", status_code=303)

    async def user_create(
        request: Request, principal: Identity = Depends(superuser)
    ) -> HTMLResponse | RedirectResponse:
        return await user_form_page(request, principal)

    async def user_edit(
        request: Request, id: int, principal: Identity = Depends(superuser)
    ) -> HTMLResponse | RedirectResponse:
        return await user_form_page(request, principal, id)

    async def user_delete(
        request: Request, id: int, principal: Identity = Depends(superuser)
    ) -> RedirectResponse:
        async with app.uow(principal=principal, request=request, source="ui") as ctx:
            await form_data(request, principal)
            user = await load_user(ctx, id)
            if user.id == principal.id:
                return RedirectResponse(f"{auth.users_url(request)}?error=self", status_code=303)
            if user.is_superuser:
                model = user_model(auth)
                active_superusers = await ctx.session.scalar(
                    sa.select(sa.func.count())
                    .select_from(model)
                    .where(model.is_superuser.is_(True), model.is_active.is_(True))
                )
                if (active_superusers or 0) <= 1:
                    return RedirectResponse(
                        f"{auth.users_url(request)}?error=last_superuser",
                        status_code=303,
                    )
            session_model = getattr(auth.backend, "session_model", None)
            if session_model is not None:
                await ctx.session.execute(
                    sa.delete(session_model).where(session_model.user_id == user.id)
                )
            await ctx.session.delete(user)
            return RedirectResponse(f"{auth.users_url(request)}?deleted=1", status_code=303)

    async def roles_page(
        request: Request, principal: Identity = Depends(superuser)
    ) -> HTMLResponse | RedirectResponse:
        async with app.uow(principal=principal, request=request, source="ui") as ctx:
            model = role_model(auth)
            roles = await all_roles(ctx)
            tree, known = permission_tree(app)
            if request.method == "GET":
                return await render(
                    request,
                    ctx,
                    "obox/auth/roles.html",
                    title=translate(request, "Roles"),
                    active="roles",
                    tree=tree,
                    new_form=role_form(None),
                    forms=[role_form(role) for role in roles],
                    saved=request.query_params.get("saved") == "1",
                    deleted=request.query_params.get("deleted") == "1",
                )

            form = await form_data(request, principal, multiple=frozenset({"permissions"}))
            errors: dict[str, list[str]] = {}
            name = str(form.get("name", "")).strip()
            selected = [str(value) for value in form.getlist("permissions")]
            if not name:
                errors["name"] = [N_("This field is required.")]
            elif len(name) > MAX_LENGTH:
                errors["name"] = [N_("Use at most 150 characters.")]
            else:
                exists = await ctx.session.scalar(sa.select(model.id).where(model.name == name))
                if exists is not None:
                    errors["name"] = [N_("This role name is already taken.")]
            if any(permission not in known for permission in selected):
                errors["permissions"] = [N_("Choose valid permissions.")]

            if errors:
                editing = role_form(
                    None,
                    values={"name": name, "permissions": set(selected)},
                    errors=errors,
                    open=True,
                )
                return await render(
                    request,
                    ctx,
                    "obox/auth/roles.html",
                    title=translate(request, "Roles"),
                    active="roles",
                    status=422,
                    tree=tree,
                    new_form=editing,
                    forms=[role_form(role) for role in roles],
                )

            ctx.session.add(model(name=name, permissions=sorted(set(selected))))
            return RedirectResponse(f"{auth.roles_url(request)}?saved=1", status_code=303)

    async def role_update(
        request: Request, id: int, principal: Identity = Depends(superuser)
    ) -> HTMLResponse | RedirectResponse:
        async with app.uow(principal=principal, request=request, source="ui") as ctx:
            model = role_model(auth)
            role = await ctx.session.get(model, id)
            if role is None:
                raise NotFound(N_("Role not found."))
            roles = await all_roles(ctx)
            tree, known = permission_tree(app)
            form = await form_data(request, principal, multiple=frozenset({"permissions"}))
            errors: dict[str, list[str]] = {}
            name = str(form.get("name", "")).strip()
            selected = [str(value) for value in form.getlist("permissions")]
            if not name:
                errors["name"] = [N_("This field is required.")]
            elif len(name) > MAX_LENGTH:
                errors["name"] = [N_("Use at most 150 characters.")]
            else:
                exists = await ctx.session.scalar(
                    sa.select(model.id).where(model.name == name, model.id != role.id)
                )
                if exists is not None:
                    errors["name"] = [N_("This role name is already taken.")]
            if any(permission not in known for permission in selected):
                errors["permissions"] = [N_("Choose valid permissions.")]

            if errors:
                editing = role_form(
                    role,
                    values={"name": name, "permissions": set(selected)},
                    errors=errors,
                    open=True,
                )
                forms = [
                    editing if item["id"] == role.id else item
                    for item in (role_form(role_item) for role_item in roles)
                ]
                return await render(
                    request,
                    ctx,
                    "obox/auth/roles.html",
                    title=translate(request, "Roles"),
                    active="roles",
                    status=422,
                    tree=tree,
                    new_form=role_form(None),
                    forms=forms,
                )

            role.name = name
            unknown = [permission for permission in role.permissions if permission not in known]
            role.permissions = unknown + sorted(set(selected))
            return RedirectResponse(f"{auth.roles_url(request)}?saved=1", status_code=303)

    async def role_delete(
        request: Request, id: int, principal: Identity = Depends(superuser)
    ) -> RedirectResponse:
        async with app.uow(principal=principal, request=request, source="ui") as ctx:
            await form_data(request, principal)
            model = role_model(auth)
            role = await ctx.session.get(model, id)
            if role is None:
                raise NotFound(N_("Role not found."))
            relation = sa.inspect(user_model(auth)).relationships.get("roles")
            if relation is not None and relation.secondary is not None:
                primary_key = sa.inspect(model).primary_key[0]
                column = next(
                    (
                        item
                        for item in relation.secondary.columns
                        if any(foreign.column is primary_key for foreign in item.foreign_keys)
                    ),
                    None,
                )
                if column is not None:
                    await ctx.session.execute(
                        sa.delete(relation.secondary).where(column == role.id)
                    )
            await ctx.session.delete(role)
            return RedirectResponse(f"{auth.roles_url(request)}?deleted=1", status_code=303)

    routes.add_api_route(
        "/users",
        user_list,
        methods=["GET"],
        name=auth.users_route,
        include_in_schema=False,
        response_model=None,
    )
    routes.add_api_route(
        "/users/new",
        user_create,
        methods=["GET", "POST"],
        name="obox_auth_user_create",
        include_in_schema=False,
        response_model=None,
    )
    routes.add_api_route(
        "/users/{id}/edit",
        user_edit,
        methods=["GET", "POST"],
        name="obox_auth_user_update",
        include_in_schema=False,
        response_model=None,
    )
    routes.add_api_route(
        "/users/{id}/delete",
        user_delete,
        methods=["POST"],
        name="obox_auth_user_delete",
        include_in_schema=False,
        response_model=None,
    )
    routes.add_api_route(
        "/roles",
        roles_page,
        methods=["GET", "POST"],
        name=auth.roles_route,
        include_in_schema=False,
        response_model=None,
    )
    routes.add_api_route(
        "/roles/{id}",
        role_update,
        methods=["POST"],
        name="obox_auth_role_update",
        include_in_schema=False,
        response_model=None,
    )
    routes.add_api_route(
        "/roles/{id}/delete",
        role_delete,
        methods=["POST"],
        name="obox_auth_role_delete",
        include_in_schema=False,
        response_model=None,
    )
    return routes
