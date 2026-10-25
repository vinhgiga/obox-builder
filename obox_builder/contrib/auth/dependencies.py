"""FastAPI dependencies: session, current user and permission gates."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from obox_builder.conf import settings
from obox_builder.contrib.auth import services, sessions
from obox_builder.contrib.auth.models import Session as AuthSession
from obox_builder.contrib.auth.models import User
from obox_builder.contrib.auth.permissions import get_user_permissions
from obox_builder.core.exceptions import NotAuthenticated, PermissionDenied
from obox_builder.db.session import get_db


async def get_auth_session(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> AuthSession | None:
    """Load the session referenced by the cookie; renew it (sliding) if needed."""
    token = request.cookies.get(settings.SESSION_COOKIE_NAME)
    if not token:
        return None
    row = await sessions.get_session(db, token)
    if row is None:
        request.state.auth_session = None
        return None
    if await sessions.touch_session(db, row):
        sessions.set_session_cookie(response, token)
    request.state.auth_session = row
    return row


async def get_current_user(
    request: Request,
    auth_session: AuthSession | None = Depends(get_auth_session),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Require an authenticated, active user; load effective permissions."""
    if auth_session is None:
        raise NotAuthenticated("Authentication required.")
    backend = auth_session.data.get("backend", settings.AUTHENTICATION_BACKENDS[0])
    user = (
        await services.get_user_for_session(db, auth_session.user_id, backend)
        if isinstance(backend, str)
        else None
    )
    if user is None or not user.is_active:
        await sessions.delete_session(db, auth_session)
        await db.commit()
        raise NotAuthenticated("Authentication required.")
    user.permissions = await get_user_permissions(db, user.id)
    request.state.user = user
    return user


async def login_required(user: User = Depends(get_current_user)) -> User:
    """Alias of :func:`get_current_user` for readable route signatures."""
    return user


def permission_required(*permissions: str) -> Callable[..., Coroutine[Any, Any, User]]:
    """Dependency factory: the user must hold at least one of ``permissions``."""

    async def dependency(user: User = Depends(get_current_user)) -> User:
        if not any(user.has_perm(permission) for permission in permissions):
            raise PermissionDenied(
                "You do not have permission to perform this action "
                f"(requires one of: {', '.join(permissions)})."
            )
        return user

    return dependency
