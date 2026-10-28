"""User creation, password management and login (with throttling)."""

from __future__ import annotations

import logging
from datetime import timedelta

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from obox_builder.conf import settings
from obox_builder.contrib.auth import sessions
from obox_builder.contrib.auth.backends import load_backend
from obox_builder.contrib.auth.hashers import (
    hash_password,
    is_usable_password,
    verify_and_update,
    verify_dummy,
    verify_password,
)
from obox_builder.contrib.auth.models import User
from obox_builder.contrib.auth.validators import PasswordValidationError, validate_password
from obox_builder.core.utils.time import ensure_aware, utcnow

logger = logging.getLogger(__name__)


def normalize_username(username: str) -> str:
    return username.strip().lower()


def normalize_email(email: str) -> str:
    return email.strip().lower()


async def get_user_by_username(db: AsyncSession, username: str) -> User | None:
    return await db.scalar(select(User).where(User.username == normalize_username(username)))


async def create_user(
    db: AsyncSession,
    username: str,
    password: str | None = None,
    *,
    email: str = "",
    full_name: str = "",
    is_superuser: bool = False,
    is_active: bool = True,
    validate: bool = True,
) -> User:
    normalized = normalize_username(username)
    if not normalized:
        raise ValueError("Username must not be empty.")
    if await get_user_by_username(db, normalized) is not None:
        raise ValueError(f"A user named {normalized!r} already exists.")
    user = User(
        username=normalized,
        email=normalize_email(email),
        full_name=full_name.strip(),
        is_superuser=is_superuser,
        is_active=is_active,
    )
    db.add(user)
    await db.flush()
    if password is not None:
        await set_password(db, user, password, validate=validate)
    return user


async def set_password(
    db: AsyncSession, user: User, raw_password: str, *, validate: bool = True
) -> int:
    """Hash and store a password, validating by default; revokes the user's sessions."""
    if validate:
        errors = validate_password(raw_password, username=user.username)
        if errors:
            raise PasswordValidationError(errors)
    user.password_hash = hash_password(raw_password)
    user.password_changed_at = utcnow()
    user.failed_login_count = 0
    user.locked_until = None
    await db.flush()
    return await sessions.revoke_user_sessions(db, user.id)


async def _authenticate_model(db: AsyncSession, username: str, password: str) -> User | None:
    """Return the user on success; throttle and constant-time behaviour on failure."""
    user = await get_user_by_username(db, username)
    now = utcnow()
    if user is None:
        verify_dummy(password)
        logger.info("Failed login for unknown username %r", username)
        return None
    if user.locked_until is not None and ensure_aware(user.locked_until) > now:
        verify_dummy(password)
        logger.info("Login attempt for locked user %r", user.username)
        return None
    valid, updated_hash = verify_and_update(password, user.password_hash)
    if not valid:
        user.failed_login_count += 1
        if user.failed_login_count >= settings.LOGIN_MAX_ATTEMPTS:
            user.locked_until = now + timedelta(seconds=settings.LOGIN_LOCKOUT_SECONDS)
            user.failed_login_count = 0
            logger.info("User %r locked until %s", user.username, user.locked_until)
        await db.flush()
        return None
    if updated_hash is not None:
        user.password_hash = updated_hash
    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = now
    await db.flush()
    return user


async def authenticate_with_backend(
    db: AsyncSession, username: str, password: str, *, request: Request | None = None
) -> tuple[User | None, str | None]:
    """Try configured backends in order and return the successful backend path."""
    for path in settings.AUTHENTICATION_BACKENDS:
        backend = load_backend(path)
        user = await backend.authenticate(db, request=request, username=username, password=password)
        if user is not None:
            return user, path
    return None, None


async def authenticate(db: AsyncSession, username: str, password: str) -> User | None:
    """Compatibility wrapper returning only the authenticated user."""
    user, _ = await authenticate_with_backend(db, username, password)
    return user


async def get_user_for_session(db: AsyncSession, user_id: int, backend_path: str) -> User | None:
    """Resolve a session user with its configured backend, as recorded at login."""
    if backend_path not in settings.AUTHENTICATION_BACKENDS:
        return None
    user = await load_backend(backend_path).get_user(db, user_id)
    return user if user is not None and user.is_active else None


async def change_password(
    db: AsyncSession, user: User, current_password: str, new_password: str
) -> int:
    """Change a password after checking the current one; revokes other sessions."""
    if not is_usable_password(user.password_hash) or not verify_password(
        current_password, user.password_hash
    ):
        raise PasswordValidationError(["Current password is incorrect."])
    return await set_password(db, user, new_password)
