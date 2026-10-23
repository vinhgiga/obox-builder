"""Dashboard view: a compact overview of the access-control data."""

from __future__ import annotations

from fastapi import Depends, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from obox_builder.contrib.admin.rendering import render
from obox_builder.contrib.admin.views.common import page_context
from obox_builder.contrib.auth.dependencies import login_required
from obox_builder.contrib.auth.models import Permission, Role, User
from obox_builder.contrib.auth.models import Session as AuthSession
from obox_builder.db.session import get_db


async def dashboard(
    request: Request,
    user: User = Depends(login_required),
    db: AsyncSession = Depends(get_db),
) -> Response:
    users = await db.scalar(select(func.count()).select_from(User))
    active_users = await db.scalar(select(func.count()).select_from(User).where(User.is_active))
    roles = await db.scalar(select(func.count()).select_from(Role))
    permissions = await db.scalar(select(func.count()).select_from(Permission))
    sessions = await db.scalar(select(func.count()).select_from(AuthSession))
    context = await page_context(
        request,
        stats={
            "users": users or 0,
            "active_users": active_users or 0,
            "roles": roles or 0,
            "permissions": permissions or 0,
            "sessions": sessions or 0,
        },
    )
    return render(request, "admin/dashboard.html", context)
