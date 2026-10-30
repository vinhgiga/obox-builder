"""Role management views: list, create, edit (permission matrix) and delete."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import Depends, Form, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from obox_builder.contrib.admin.context import AdminContext, current_site
from obox_builder.contrib.admin.rendering import render
from obox_builder.contrib.admin.services.history import (
    before_cursor,
    build_card,
    diff_mask,
    model_timeline_available,
    timeline_context,
)
from obox_builder.contrib.admin.views.common import ensure_csrf, flash, page_context
from obox_builder.contrib.auth.dependencies import permission_required
from obox_builder.contrib.auth.models import Permission, Role, User, user_roles
from obox_builder.contrib.auth.permissions import collect_permissions
from obox_builder.contrib.history import HISTORY_VIEW_PERMISSION
from obox_builder.db.session import get_db
from obox_builder.http.shortcuts import redirect
from obox_builder.resources import ResourceValidationError, get_registry

logger = logging.getLogger(__name__)

ViewRoles = Annotated[User, Depends(permission_required("auth.roles.read"))]
AddRoles = Annotated[User, Depends(permission_required("auth.roles.manage"))]
ChangeRoles = Annotated[User, Depends(permission_required("auth.roles.manage"))]
DeleteRoles = Annotated[User, Depends(permission_required("auth.roles.manage"))]
HistoryRoles = Annotated[User, Depends(permission_required(HISTORY_VIEW_PERMISSION))]

CRUD_ACTIONS = ("read", "create", "update", "delete", "manage")


def _history_visible(user: User) -> bool:
    return (
        model_timeline_available(Role)
        and user.has_perm("auth.roles.read")
        and user.has_perm(HISTORY_VIEW_PERMISSION)
    )


async def _history_for(db: AsyncSession, user: User, target: Role) -> dict[str, object] | None:
    if not _history_visible(user):
        return None
    return await build_card(
        db,
        Role,
        target.id,
        url=f"/admin/roles/{target.id}/history",
        mask=diff_mask(Role, user=user),
    )


async def _permission_matrix(db: AsyncSession) -> list[dict[str, object]]:
    """Group declared grants by service and resource."""
    declared = {permission.name: permission for permission in collect_permissions()}
    rows = await db.scalars(
        select(Permission).order_by(Permission.service, Permission.resource, Permission.action)
    )
    services: dict[str, dict[str, list[Permission]]] = {}
    for permission in rows:
        services.setdefault(permission.service, {}).setdefault(permission.resource, []).append(
            permission
        )
    matrix: list[dict[str, object]] = []
    for service, resources in services.items():
        models: list[dict[str, object]] = []
        for resource, permissions in resources.items():
            crud: dict[str, Permission | None] = dict.fromkeys(CRUD_ACTIONS)
            extras: list[dict[str, object]] = []
            for permission in permissions:
                if permission.action in CRUD_ACTIONS:
                    crud[permission.action] = permission
                else:
                    extras.append(
                        {
                            "id": permission.id,
                            "label": (
                                declared[permission.name].label
                                if permission.name in declared
                                else f"{permission.action} (retired)"
                            ),
                        }
                    )
            models.append({"name": resource, "crud": crud, "extras": extras})
        matrix.append({"service": service, "models": models})
    return matrix


async def role_list(
    request: Request,
    user: ViewRoles,
    db: AsyncSession = Depends(get_db),
) -> Response:
    roles = list(
        await db.scalars(select(Role).options(selectinload(Role.permissions)).order_by(Role.name))
    )
    user_counts_rows = await db.execute(
        select(user_roles.c.role_id, func.count()).group_by(user_roles.c.role_id)
    )
    user_counts = {row[0]: row[1] for row in user_counts_rows.all()}
    context = await page_context(
        request,
        roles=roles,
        user_counts=user_counts,
        start_index=1,
        show_history=_history_visible(user),
    )
    return render(request, "admin/roles/list.html", context)


async def role_create(
    request: Request,
    user: AddRoles,
    db: AsyncSession = Depends(get_db),
) -> Response:
    return render(request, "admin/roles/form.html", await page_context(request, target=None))


async def role_create_submit(
    request: Request,
    user: AddRoles,
    csrf_token: str = Form(""),
    name: str = Form(""),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await ensure_csrf(request, csrf_token)
    errors: list[str] = []
    resource = get_registry().for_model(Role)
    ctx = AdminContext(request, user, db, current_site(request)).operation_context()
    try:
        result = await resource.execute("create", ctx, {"name": name})
    except ResourceValidationError as exc:
        errors = [message for values in exc.errors.values() for message in values]
    else:
        await flash(request, f"Role {name!r} created.")
        return redirect(f"/admin/roles/{result.id}", status_code=303)
    context = {"target": None, "errors": errors, "form": {"name": name}}
    return render(
        request, "admin/roles/form.html", await page_context(request, **context), status_code=200
    )


async def role_edit(
    request: Request,
    role_id: int,
    user: ChangeRoles,
    db: AsyncSession = Depends(get_db),
) -> Response:
    role = await db.scalar(
        select(Role).where(Role.id == role_id).options(selectinload(Role.permissions))
    )
    if role is None:
        return render(request, "admin/errors/404.html", {}, status_code=404)
    context = {
        "target": role,
        "matrix": await _permission_matrix(db),
        "selected_permission_ids": {permission.id for permission in role.permissions},
        "history": await _history_for(db, user, role),
    }
    return render(request, "admin/roles/form.html", await page_context(request, **context))


async def role_history(
    request: Request,
    role_id: int,
    user: ViewRoles,
    history_reader: HistoryRoles,
    db: AsyncSession = Depends(get_db),
) -> Response:
    role = await db.get(Role, role_id)
    if role is None:
        return render(request, "admin/errors/404.html", {}, status_code=404)
    context = await page_context(
        request,
        **await timeline_context(
            db,
            Role,
            role.id,
            before=before_cursor(request),
            heading="Role history",
            subtitle=role.name,
            back_url=f"/admin/roles/{role.id}",
            timeline_url=request.url.path,
            mask=diff_mask(Role, user=user),
        ),
    )
    return render(request, "admin/history.html", context)


async def role_edit_submit(
    request: Request,
    role_id: int,
    user: ChangeRoles,
    csrf_token: str = Form(""),
    name: str = Form(""),
    permission_ids: list[int] = Form(default=[]),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await ensure_csrf(request, csrf_token)
    role = await db.scalar(
        select(Role)
        .where(Role.id == role_id)
        .options(selectinload(Role.permissions))
        .with_for_update()
    )
    if role is None:
        return render(request, "admin/errors/404.html", {}, status_code=404)
    resource = get_registry().for_model(Role)
    ctx = AdminContext(request, user, db, current_site(request)).operation_context()
    errors = []
    try:
        await resource.execute(
            "update",
            ctx,
            {"name": name, "permission_ids": list(map(str, permission_ids))},
            target=role_id,
        )
    except ResourceValidationError as exc:
        errors = [message for values in exc.errors.values() for message in values]
    else:
        await flash(request, f"Role {role.name!r} updated.")
        return redirect("/admin/roles", status_code=303)
    context = {
        "target": role,
        "errors": errors,
        "form": {"name": name},
        "matrix": await _permission_matrix(db),
        "selected_permission_ids": set(permission_ids),
        "history": await _history_for(db, user, role),
    }
    return render(
        request, "admin/roles/form.html", await page_context(request, **context), status_code=200
    )


async def role_delete(
    request: Request,
    role_id: int,
    user: DeleteRoles,
    csrf_token: str = Form(""),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await ensure_csrf(request, csrf_token)
    role = await db.scalar(
        select(Role)
        .where(Role.id == role_id)
        .options(selectinload(Role.permissions))
        .with_for_update()
    )
    if role is None:
        return render(request, "admin/errors/404.html", {}, status_code=404)
    resource = get_registry().for_model(Role)
    ctx = AdminContext(request, user, db, current_site(request)).operation_context()
    await resource.execute("delete", ctx, target=role_id)
    logger.info("Admin %r deleted role %r", user.username, role.name)
    await flash(request, f"Role {role.name!r} deleted.")
    return redirect("/admin/roles", status_code=303)
