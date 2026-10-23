"""User management views: list with search, create, edit (roles, password, state)."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import Depends, Form, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from obox_builder.contrib.admin.rendering import render
from obox_builder.contrib.admin.views.common import PAGE_SIZE, ensure_csrf, flash, page_context
from obox_builder.contrib.admin.views.history import (
    before_cursor,
    build_card,
    diff_mask,
    model_timeline_available,
    timeline_context,
)
from obox_builder.contrib.auth import services
from obox_builder.contrib.auth.dependencies import permission_required
from obox_builder.contrib.auth.models import Role, User, user_roles
from obox_builder.contrib.auth.validators import PasswordValidationError
from obox_builder.contrib.history import HISTORY_VIEW_PERMISSION
from obox_builder.db.session import get_db
from obox_builder.http.shortcuts import redirect

logger = logging.getLogger(__name__)

ViewUsers = Annotated[User, Depends(permission_required("auth.view_user"))]
AddUsers = Annotated[User, Depends(permission_required("auth.add_user"))]
ChangeUsers = Annotated[User, Depends(permission_required("auth.change_user"))]
HistoryUsers = Annotated[User, Depends(permission_required(HISTORY_VIEW_PERMISSION))]


def _history_visible(user: User) -> bool:
    return (
        model_timeline_available(User)
        and user.has_perm("auth.view_user")
        and user.has_perm(HISTORY_VIEW_PERMISSION)
    )


async def _history_for(db: AsyncSession, user: User, target: User) -> dict[str, object] | None:
    if not _history_visible(user):
        return None
    return await build_card(
        db,
        User,
        target.id,
        url=f"/admin/users/{target.id}/history",
        mask=diff_mask(User, user=user),
    )


async def _all_roles(db: AsyncSession) -> list[Role]:
    return list(await db.scalars(select(Role).order_by(Role.name)))


async def _active_superuser_count(db: AsyncSession, *, excluding: User | None = None) -> int:
    query = (
        select(func.count())
        .select_from(User)
        .where(User.is_superuser.is_(True), User.is_active.is_(True))
    )
    if excluding is not None:
        query = query.where(User.id != excluding.id)
    return await db.scalar(query) or 0


def _user_form_context(user: User | None, roles: list[Role], **extra: object) -> dict[str, object]:
    context: dict[str, object] = {"target": user, "roles": roles}
    context.update(extra)
    return context


async def user_list(
    request: Request,
    user: ViewUsers,
    db: AsyncSession = Depends(get_db),
) -> Response:
    query_text = request.query_params.get("q", "").strip()
    page = max(int(request.query_params.get("page", "1") or "1"), 1)

    statement = select(User).order_by(User.username)
    counter = select(func.count()).select_from(User)
    if query_text:
        pattern = f"%{query_text.lower()}%"
        condition = func.lower(User.username).like(pattern) | func.lower(User.email).like(pattern)
        statement = statement.where(condition)
        counter = counter.where(condition)

    total = await db.scalar(counter) or 0
    pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    page = min(page, pages)
    users = list(await db.scalars(statement.limit(PAGE_SIZE).offset((page - 1) * PAGE_SIZE)))
    roles_by_user: dict[int, int] = {}
    if users:
        rows = await db.execute(
            select(user_roles.c.user_id, func.count())
            .where(user_roles.c.user_id.in_([item.id for item in users]))
            .group_by(user_roles.c.user_id)
        )
        roles_by_user = {row[0]: row[1] for row in rows.all()}
    context = await page_context(
        request,
        users=users,
        roles_by_user=roles_by_user,
        q=query_text,
        page=page,
        pages=pages,
        start_index=(page - 1) * PAGE_SIZE + 1,
        total=total,
        show_history=_history_visible(user),
    )
    return render(request, "admin/users/list.html", context)


async def user_create(
    request: Request,
    user: AddUsers,
    db: AsyncSession = Depends(get_db),
) -> Response:
    roles = await _all_roles(db)
    return render(
        request,
        "admin/users/form.html",
        await page_context(request, **_user_form_context(None, roles)),
    )


async def user_create_submit(
    request: Request,
    user: AddUsers,
    csrf_token: str = Form(""),
    username: str = Form(""),
    email: str = Form(""),
    full_name: str = Form(""),
    password: str = Form(""),
    confirm_password: str = Form(""),
    is_active: bool = Form(False),
    is_superuser: bool = Form(False),
    role_ids: list[int] = Form(default=[]),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await ensure_csrf(request, csrf_token)
    roles = await _all_roles(db)
    errors: list[str] = []
    if not password:
        errors.append("A password is required.")
    elif password != confirm_password:
        errors.append("The two password fields do not match.")
    if is_superuser and not user.is_superuser:
        errors.append("Only a superuser can create another superuser.")
    if not errors:
        try:
            target = await services.create_user(
                db,
                username,
                password or None,
                email=email,
                full_name=full_name,
                is_superuser=is_superuser and user.is_superuser,
                is_active=is_active,
            )
        except (ValueError, PasswordValidationError) as exc:
            errors = getattr(exc, "errors", None) or [str(exc)]
        else:
            selected = [role for role in roles if role.id in set(role_ids)]
            if selected:
                await db.refresh(target, ["roles"])
                target.roles = selected
            logger.info("Admin %r created user %r", user.username, target.username)
            await flash(request, f"User {target.username!r} created.")
            return redirect("/admin/users", status_code=303)

    context = _user_form_context(
        None,
        roles,
        errors=errors,
        selected_role_ids=set(role_ids),
        form={
            "username": username,
            "email": email,
            "full_name": full_name,
            "is_active": is_active,
            "is_superuser": is_superuser,
        },
    )
    return render(
        request, "admin/users/form.html", await page_context(request, **context), status_code=200
    )


async def user_edit(
    request: Request,
    user_id: int,
    user: ChangeUsers,
    db: AsyncSession = Depends(get_db),
) -> Response:
    target = await db.scalar(
        select(User).where(User.id == user_id).options(selectinload(User.roles))
    )
    if target is None:
        return render(request, "admin/errors/404.html", {}, status_code=404)
    roles = await _all_roles(db)
    context = _user_form_context(
        target,
        roles,
        selected_role_ids={role.id for role in target.roles},
        history=await _history_for(db, user, target),
    )
    return render(request, "admin/users/form.html", await page_context(request, **context))


async def user_history(
    request: Request,
    user_id: int,
    user: ViewUsers,
    history_reader: HistoryUsers,
    db: AsyncSession = Depends(get_db),
) -> Response:
    target = await db.get(User, user_id)
    if target is None:
        return render(request, "admin/errors/404.html", {}, status_code=404)
    context = await page_context(
        request,
        **await timeline_context(
            db,
            User,
            target.id,
            before=before_cursor(request),
            heading="User history",
            subtitle=target.username,
            back_url=f"/admin/users/{target.id}",
            timeline_url=request.url.path,
            mask=diff_mask(User, user=user),
        ),
    )
    return render(request, "admin/history.html", context)


async def user_edit_submit(
    request: Request,
    user_id: int,
    user: ChangeUsers,
    csrf_token: str = Form(""),
    email: str = Form(""),
    full_name: str = Form(""),
    password: str = Form(""),
    confirm_password: str = Form(""),
    is_active: bool = Form(False),
    is_superuser: bool = Form(False),
    role_ids: list[int] = Form(default=[]),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await ensure_csrf(request, csrf_token)
    target = await db.scalar(
        select(User).where(User.id == user_id).options(selectinload(User.roles))
    )
    if target is None:
        return render(request, "admin/errors/404.html", {}, status_code=404)
    roles = await _all_roles(db)
    errors: list[str] = []

    if is_superuser != target.is_superuser and not user.is_superuser:
        errors.append("Only a superuser can change the superuser flag.")
    wants_superuser = target.is_superuser if not user.is_superuser else is_superuser
    if (
        target.is_superuser
        and (not wants_superuser or not is_active)
        and await _active_superuser_count(db, excluding=target) == 0
    ):
        errors.append("The last active superuser cannot be demoted or deactivated.")
    if password and password != confirm_password:
        errors.append("The two password fields do not match.")

    if not errors:
        target.email = services.normalize_email(email)
        target.full_name = full_name.strip()
        target.is_active = is_active
        if user.is_superuser:
            target.is_superuser = is_superuser
        if password:
            try:
                await services.set_password(db, target, password)
            except PasswordValidationError as exc:
                errors = exc.errors
        if not errors:
            selected = [role for role in roles if role.id in set(role_ids)]
            target.roles = selected
            if not target.is_active:
                from obox_builder.contrib.auth import sessions as auth_sessions

                await auth_sessions.revoke_user_sessions(db, target.id)
            logger.info("Admin %r updated user %r", user.username, target.username)
            await flash(request, f"User {target.username!r} updated.")
            return redirect("/admin/users", status_code=303)

    context = _user_form_context(
        target,
        roles,
        errors=errors,
        selected_role_ids={role.id for role in target.roles},
        form={
            "email": email,
            "full_name": full_name,
            "is_active": is_active,
            "is_superuser": is_superuser,
        },
        history=await _history_for(db, user, target),
    )
    return render(
        request, "admin/users/form.html", await page_context(request, **context), status_code=200
    )
