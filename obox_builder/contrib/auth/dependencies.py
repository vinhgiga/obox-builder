"""FastAPI dependencies: session, current user and permission gates."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import Depends, Request, Response
from fastapi.params import Depends as DependsParameter
from sqlalchemy.ext.asyncio import AsyncSession

from obox_builder.conf import settings
from obox_builder.contrib.auth import services, sessions
from obox_builder.contrib.auth.models import Session as AuthSession
from obox_builder.contrib.auth.models import User
from obox_builder.contrib.auth.permissions import get_actor, validate_permission_references
from obox_builder.db.session import get_db
from obox_builder.exceptions import NotAuthenticated, PermissionDenied
from obox_builder.security import Actor, PermissionRef


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
        request.state.session_renewal_cookie = response.headers.getlist("set-cookie")[-1]
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
    request.state.actor = await get_actor(db, user)
    user.permissions = request.state.actor.permissions
    request.state.user = user
    return user


async def login_required(user: User = Depends(get_current_user)) -> User:
    """Alias of :func:`get_current_user` for readable route signatures."""
    return user


async def get_current_actor(request: Request, user: User = Depends(get_current_user)) -> Actor:
    return request.state.actor


CurrentUser = Annotated[User, Depends(get_current_user)]
CurrentActor = Annotated[Actor, Depends(get_current_actor)]


def require_permission(name: str) -> DependsParameter:
    return Depends(permission_required(name))


def permission_required(*permissions: str) -> Callable[..., Coroutine[Any, Any, User]]:
    """Require every listed permission."""
    for name in permissions:
        PermissionRef.parse(name)

    async def dependency(
        user: User = Depends(get_current_user), actor: Actor = Depends(get_current_actor)
    ) -> User:
        validate_permission_references(permissions)
        if not all(actor.has_permission(permission) for permission in permissions):
            raise PermissionDenied(
                "You do not have permission to perform this action "
                f"(requires all of: {', '.join(permissions)})."
            )
        return user

    return dependency


def any_permission_required(*permissions: str) -> Callable[..., Coroutine[Any, Any, User]]:
    """Require at least one of the listed permissions."""
    for name in permissions:
        PermissionRef.parse(name)

    async def dependency(
        user: User = Depends(get_current_user), actor: Actor = Depends(get_current_actor)
    ) -> User:
        validate_permission_references(permissions)
        if not any(actor.has_permission(permission) for permission in permissions):
            raise PermissionDenied("You do not have permission to perform this action.")
        return user

    return dependency
