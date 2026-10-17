"""Role management views: list, create, edit (permission matrix) and delete."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import Depends, Form, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from obox_builder.contrib.admin.views.common import ensure_csrf, flash, page_context
from obox_builder.contrib.auth.dependencies import permission_required
from obox_builder.contrib.auth.models import Permission, Role, User, user_roles
from obox_builder.contrib.auth.permissions import collect_permissions
from obox_builder.db.session import get_db
from obox_builder.http.shortcuts import redirect, render

logger = logging.getLogger(__name__)

ViewRoles = Annotated[User, Depends(permission_required("auth.view_role"))]
AddRoles = Annotated[User, Depends(permission_required("auth.add_role"))]
ChangeRoles = Annotated[User, Depends(permission_required("auth.change_role"))]
DeleteRoles = Annotated[User, Depends(permission_required("auth.delete_role"))]

CRUD_ACTIONS = ("view", "add", "change", "delete")


async def _permission_matrix(db: AsyncSession) -> list[dict[str, object]]:
    """Group permissions by app -> model with CRUD columns plus custom extras."""
    rows = list(
        await db.scalars(select(Permission).order_by(Permission.app_label, Permission.model))
    )
    by_id = {(row.app_label, row.codename): row for row in rows}
    declared = collect_permissions()

    apps: dict[str, dict[str, list[Permission]]] = {}
    for entry in declared:
        row = by_id.get((entry.app_label, entry.codename))
        if row is None:
            continue
        apps.setdefault(entry.app_label, {}).setdefault(entry.model or "", []).append(row)

    matrix: list[dict[str, object]] = []
    for app_label in sorted(apps):
        models: list[dict[str, object]] = []
        for model in sorted(apps[app_label]):
            permissions = apps[app_label][model]
            crud: dict[str, Permission | None] = dict.fromkeys(CRUD_ACTIONS)
            extras: list[Permission] = []
            for permission in permissions:
                action, _, subject = permission.codename.partition("_")
                if model and action in CRUD_ACTIONS and subject == model:
                    crud[action] = permission
                else:
                    extras.append(permission)
            models.append({"name": model or "(other)", "crud": crud, "extras": extras})
        matrix.append({"app_label": app_label, "models": models})
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
    description: str = Form(""),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await ensure_csrf(request, csrf_token)
    errors: list[str] = []
    cleaned = name.strip()
    if not cleaned:
        errors.append("A role name is required.")
    elif await db.scalar(select(Role).where(Role.name == cleaned)) is not None:
        errors.append(f"A role named {cleaned!r} already exists.")
    if not errors:
        role = Role(name=cleaned, description=description.strip())
        db.add(role)
        await db.flush()
        logger.info("Admin %r created role %r", user.username, role.name)
        await flash(request, f"Role {role.name!r} created.")
        return redirect(f"/admin/roles/{role.id}", status_code=303)
    context = {"target": None, "errors": errors, "form": {"name": name, "description": description}}
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
    }
    return render(request, "admin/roles/form.html", await page_context(request, **context))


async def role_edit_submit(
    request: Request,
    role_id: int,
    user: ChangeRoles,
    csrf_token: str = Form(""),
    name: str = Form(""),
    description: str = Form(""),
    permission_ids: list[int] = Form(default=[]),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await ensure_csrf(request, csrf_token)
    role = await db.scalar(
        select(Role).where(Role.id == role_id).options(selectinload(Role.permissions))
    )
    if role is None:
        return render(request, "admin/errors/404.html", {}, status_code=404)
    errors: list[str] = []
    cleaned = name.strip()
    if not cleaned:
        errors.append("A role name is required.")
    else:
        duplicate = await db.scalar(select(Role).where(Role.name == cleaned, Role.id != role.id))
        if duplicate is not None:
            errors.append(f"A role named {cleaned!r} already exists.")
    if not errors:
        role.name = cleaned
        role.description = description.strip()
        selected = set(permission_ids)
        permissions = list(await db.scalars(select(Permission)))
        role.permissions = [perm for perm in permissions if perm.id in selected]
        logger.info(
            "Admin %r updated role %r (%d permissions)",
            user.username,
            role.name,
            len(role.permissions),
        )
        await flash(request, f"Role {role.name!r} updated.")
        return redirect("/admin/roles", status_code=303)
    context = {
        "target": role,
        "errors": errors,
        "form": {"name": name, "description": description},
        "matrix": await _permission_matrix(db),
        "selected_permission_ids": set(permission_ids),
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
    role = await db.get(Role, role_id)
    if role is None:
        return render(request, "admin/errors/404.html", {}, status_code=404)
    if role.is_system:
        await flash(request, f"Role {role.name!r} is a system role and cannot be deleted.", "error")
        return redirect("/admin/roles", status_code=303)
    await db.delete(role)
    logger.info("Admin %r deleted role %r", user.username, role.name)
    await flash(request, f"Role {role.name!r} deleted.")
    return redirect("/admin/roles", status_code=303)
