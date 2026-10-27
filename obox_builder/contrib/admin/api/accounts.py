"""Explicit account representations and guarded administration."""

from __future__ import annotations

from datetime import timezone
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Path, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import selectinload

from obox_builder.contrib.admin.api.auth import parse_body
from obox_builder.contrib.admin.api.deps import DB, USER, json_body
from obox_builder.contrib.admin.api.etag import check_if_match, etag
from obox_builder.contrib.admin.api.problems import Problem
from obox_builder.contrib.admin.api.records import body_schema
from obox_builder.contrib.auth import administration, services, sessions
from obox_builder.contrib.auth.models import Permission, Role, User
from obox_builder.contrib.auth.permissions import collect_permissions
from obox_builder.contrib.auth.tokens import scope_allows
from obox_builder.contrib.auth.validators import PasswordValidationError
from obox_builder.core.exceptions import PermissionDenied
from obox_builder.core.utils.time import ensure_aware, utcnow

AccountID = Annotated[int, Path(ge=1, le=2**63 - 1)]


class UserInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    username: str = Field(min_length=1, max_length=150)
    password: str = Field(min_length=1, max_length=4096)
    email: str = Field(default="", max_length=254)
    full_name: str = Field(default="", max_length=255)
    is_active: bool = True
    is_superuser: bool = False
    role_ids: list[str] = Field(default_factory=list, max_length=200)


class UserPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    password: str = Field(default="", min_length=1, max_length=4096)
    email: str = Field(default="", max_length=254)
    full_name: str = Field(default="", max_length=255)
    is_active: bool = True
    is_superuser: bool = False
    role_ids: list[str] = Field(default_factory=list, max_length=200)


class RoleInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(default="", min_length=1, max_length=150)
    description: str = Field(default="", max_length=10000)
    permission_ids: list[str] = Field(default_factory=list, max_length=1000)


class RoleCreate(RoleInput):
    name: str = Field(min_length=1, max_length=150)


def ids(values: list[str]) -> list[int]:
    if any(
        not value.isascii()
        or not value.isdecimal()
        or len(value) > 19
        or not 0 < int(value) < 2**63
        for value in values
    ):
        raise Problem(422, "validation_error", "IDs must be positive decimal strings.")
    return list(dict.fromkeys(int(value) for value in values))


def account_access(request: Request, user: User, kind: str, action: str) -> User:
    if not request.state.admin_site.api_auth_enabled:
        raise Problem(404, "not_found", "Account API is unavailable.")
    token = getattr(request.state, "api_token", None)
    operation = {"view": "read", "add": "write", "change": "write", "delete": "delete"}[action]
    if not user.has_perm(f"auth.{action}_{kind}") or (
        token is not None and not scope_allows(token.scopes, f"auth.{kind}", operation)
    ):
        raise PermissionDenied("You cannot administer this resource.")
    if token is None:
        return user
    # A bearer may delegate only permissions represented by its own scopes.
    actor = User(is_superuser=False)
    effective = set()
    held = (
        {f"{p.app_label}.{p.codename}" for p in collect_permissions()}
        if user.is_superuser
        else user.permissions
    )
    for permission in held:
        app, _, codename = permission.partition(".")
        action, _, model = codename.partition("_")
        granted_operation = {
            "view": "read",
            "add": "write",
            "change": "write",
            "delete": "delete",
        }.get(action)
        if granted_operation and scope_allows(token.scopes, f"{app}.{model}", granted_operation):
            effective.add(permission)
    actor.permissions = frozenset(effective)
    return actor


def account_data(row: Any) -> dict[str, Any]:
    if isinstance(row, User):
        data = {
            name: getattr(row, name)
            for name in ("username", "email", "full_name", "is_active", "is_superuser")
        }
        data["role_ids"] = sorted(str(role.id) for role in row.roles)
        # Password changes invalidate validators without exposing credentials.
        revision = [
            row.version,
            ensure_aware(row.updated_at).astimezone(timezone.utc).isoformat(),
            ensure_aware(row.password_changed_at).astimezone(timezone.utc).isoformat()
            if row.password_changed_at
            else None,
        ]
    else:
        data = {
            "name": row.name,
            "description": row.description,
            "is_system": row.is_system,
            "permission_ids": sorted(str(p.id) for p in row.permissions),
        }
        revision = [row.version, ensure_aware(row.updated_at).astimezone(timezone.utc).isoformat()]
    return {"id": str(row.id), **data, "etag": etag(data, revision)}


async def load_account(db: Any, model: Any, pk: int, *, lock: bool = False) -> Any:
    relation = User.roles if model is User else Role.permissions
    loader = selectinload(relation)
    if model is User:
        loader = loader.selectinload(Role.permissions)
    stmt = sa.select(model).where(model.id == pk).options(loader)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    row = await db.scalar(stmt)
    if row is None:
        raise Problem(404, "not_found", "Account resource is unavailable.")
    return row


def response(row: Any, status: int = 200) -> JSONResponse:
    data = account_data(row)
    return JSONResponse(data, status_code=status, headers={"ETag": data["etag"]})


async def roles_for(db: Any, actor: User, values: list[str]) -> list[Role]:
    try:
        return await administration.selected_roles(db, actor, ids(values))
    except ValueError as exc:
        raise Problem(422, "validation_error", str(exc)) from exc


def register_accounts(router: APIRouter) -> None:
    @router.get("/users")
    async def users(
        request: Request,
        user: USER,
        db: DB,
        page: int = Query(1, ge=1, le=100000),
        size: int = Query(50, ge=1, le=200),
        q: str = Query("", max_length=200),
    ) -> Any:
        account_access(request, user, "user", "view")
        stmt = sa.select(User).options(selectinload(User.roles)).order_by(User.username, User.id)
        if q:
            stmt = stmt.where(
                sa.or_(
                    User.username.icontains(q, autoescape=True),
                    User.email.icontains(q, autoescape=True),
                )
            )
        rows = list(await db.scalars(stmt.offset((page - 1) * size).limit(size + 1)))
        return {"users": [account_data(row) for row in rows[:size]], "has_next": len(rows) > size}

    @router.get("/users/{pk}")
    async def get_user(request: Request, pk: AccountID, user: USER, db: DB) -> Any:
        account_access(request, user, "user", "view")
        return response(await load_account(db, User, pk))

    @router.post("/users", status_code=201, openapi_extra=body_schema(UserInput))
    async def create_user(request: Request, user: USER, db: DB) -> Any:
        actor = account_access(request, user, "user", "add")
        data = parse_body(UserInput, await json_body(request))
        await administration.check_user(
            db, actor, None, active=data.is_active, superuser=data.is_superuser
        )
        roles = await roles_for(db, actor, data.role_ids)
        try:
            target = await services.create_user(
                db,
                data.username,
                data.password,
                email=data.email,
                full_name=data.full_name,
                is_active=data.is_active,
                is_superuser=data.is_superuser,
            )
        except (ValueError, PasswordValidationError) as exc:
            raise Problem(422, "validation_error", str(exc)) from exc
        await db.refresh(target, ["roles"])
        target.roles = roles
        await db.flush()
        await db.refresh(target)
        return response(await load_account(db, User, target.id), 201)

    @router.patch("/users/{pk}", openapi_extra=body_schema(UserPatch))
    async def update_user(request: Request, pk: AccountID, user: USER, db: DB) -> Any:
        actor = account_access(request, user, "user", "change")
        data = parse_body(UserPatch, await json_body(request)).model_dump(exclude_unset=True)
        await administration.lock_superusers(db)
        target = await load_account(db, User, pk, lock=True)
        check_if_match(account_data(target)["etag"], request.headers.get("if-match"))
        await administration.check_user(
            db,
            actor,
            target,
            active=data.get("is_active", target.is_active),
            superuser=data.get("is_superuser", target.is_superuser),
        )
        if "role_ids" in data:
            target.roles = await roles_for(db, actor, data.pop("role_ids"))
        password = data.pop("password", None)
        if password:
            try:
                await services.set_password(db, target, password)
            except PasswordValidationError as exc:
                raise Problem(422, "validation_error", str(exc)) from exc
        for name, value in data.items():
            setattr(target, name, services.normalize_email(value) if name == "email" else value)
        target.updated_at = utcnow()
        if not target.is_active:
            await sessions.revoke_user_sessions(db, target.id)
        await db.flush()
        return response(target)

    @router.get("/roles")
    async def roles(
        request: Request,
        user: USER,
        db: DB,
        page: int = Query(1, ge=1, le=100000),
        size: int = Query(50, ge=1, le=200),
    ) -> Any:
        account_access(request, user, "role", "view")
        rows = list(
            await db.scalars(
                sa.select(Role)
                .options(selectinload(Role.permissions))
                .order_by(Role.name, Role.id)
                .offset((page - 1) * size)
                .limit(size + 1)
            )
        )
        return {"roles": [account_data(row) for row in rows[:size]], "has_next": len(rows) > size}

    @router.get("/permissions")
    async def permissions(request: Request, user: USER, db: DB) -> Any:
        actor = account_access(request, user, "role", "view")
        rows = list(await db.scalars(sa.select(Permission).order_by(Permission.id)))
        return {
            "permissions": [
                {"id": str(p.id), "name": p.name, "permission": p.full_name}
                for p in rows
                if actor.has_perm(p.full_name)
            ]
        }

    @router.get("/roles/{pk}")
    async def get_role(request: Request, pk: AccountID, user: USER, db: DB) -> Any:
        account_access(request, user, "role", "view")
        return response(await load_account(db, Role, pk))

    async def save_role(request: Request, user: User, db: Any, pk: int | None) -> Any:
        actor = account_access(request, user, "role", "add" if pk is None else "change")
        data = parse_body(RoleInput, await json_body(request)).model_dump(exclude_unset=True)
        role = (
            Role(name="", description="", permissions=[])
            if pk is None
            else await load_account(db, Role, pk, lock=True)
        )
        if pk is not None:
            check_if_match(account_data(role)["etag"], request.headers.get("if-match"))
            administration.check_role(actor, role)
        if "name" in data:
            data["name"] = data["name"].strip()
        if not data.get("name", role.name):
            raise Problem(422, "validation_error", "A role name is required.")
        if "permission_ids" in data:
            selected = ids(data.pop("permission_ids"))
            perms = list(await db.scalars(sa.select(Permission).where(Permission.id.in_(selected))))
            if {p.id for p in perms} != set(selected):
                raise Problem(422, "validation_error", "Unknown permission.")
            administration.check_permissions(actor, perms)
            role.permissions = perms
        for name, value in data.items():
            setattr(role, name, value)
        role.updated_at = utcnow()
        db.add(role)
        await db.flush()
        return response(role, 201 if pk is None else 200)

    @router.post("/roles", status_code=201, openapi_extra=body_schema(RoleCreate))
    async def create_role(request: Request, user: USER, db: DB) -> Any:
        return await save_role(request, user, db, None)

    @router.patch("/roles/{pk}", openapi_extra=body_schema(RoleInput))
    async def update_role(request: Request, pk: AccountID, user: USER, db: DB) -> Any:
        return await save_role(request, user, db, pk)

    @router.delete("/roles/{pk}", status_code=204)
    async def delete_role(request: Request, pk: AccountID, user: USER, db: DB) -> Response:
        actor = account_access(request, user, "role", "delete")
        role = await load_account(db, Role, pk, lock=True)
        check_if_match(account_data(role)["etag"], request.headers.get("if-match"))
        administration.check_role(actor, role)
        if role.is_system:
            raise Problem(409, "conflict", "System roles cannot be deleted.")
        await db.delete(role)
        return Response(status_code=204)
