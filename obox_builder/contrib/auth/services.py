"""User creation, password management and login."""

from __future__ import annotations

import logging
from asyncio import to_thread
from typing import Any, cast

from fastapi import Request
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import String, delete, select
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
from obox_builder.contrib.auth.models import PasswordResetToken, User
from obox_builder.contrib.auth.validators import PasswordValidationError, validate_password
from obox_builder.core.utils.time import utcnow

logger = logging.getLogger(__name__)


def normalize_username(username: str) -> str:
    return username.strip().lower()


def normalize_email(email: str) -> str:
    return email.strip().lower()


def _identifier_value(username: str) -> Any:
    raw = normalize_username(username)
    try:
        return TypeAdapter(
            User.__table__.columns[User.USERNAME_FIELD].type.python_type
        ).validate_python(raw)
    except (NotImplementedError, TypeError, ValidationError) as exc:
        raise ValueError("Invalid user identifier.") from exc


async def get_user_by_username(db: AsyncSession, username: str) -> User | None:
    identifier = getattr(User, User.USERNAME_FIELD)
    try:
        value = _identifier_value(username)
    except ValueError:
        return None
    return await db.scalar(select(User).where(identifier == value))


async def create_user(
    db: AsyncSession,
    username: str = "",
    password: str | None = None,
    *,
    email: str = "",
    full_name: str = "",
    is_superuser: bool = False,
    is_active: bool = True,
    validate: bool = True,
    **extra_fields: Any,
) -> User:
    field_name = User.USERNAME_FIELD
    supplied_identifier = extra_fields.pop(field_name, "")
    if field_name == User.EMAIL_FIELD and email:
        if supplied_identifier and normalize_username(str(supplied_identifier)) != normalize_email(
            email
        ):
            raise ValueError(f"Conflicting values for {field_name!r}.")
        supplied_identifier = email
    supplied_identifier = "" if supplied_identifier is None else str(supplied_identifier)
    if (
        username
        and supplied_identifier
        and normalize_username(username) != normalize_username(supplied_identifier)
    ):
        raise ValueError(f"Conflicting values for {field_name!r}.")
    normalized = normalize_username(username or supplied_identifier)
    if not normalized:
        raise ValueError("Username must not be empty.")
    column_type = User.__table__.columns[field_name].type
    if (
        isinstance(column_type, String)
        and column_type.length
        and len(normalized) > column_type.length
    ):
        raise ValueError(f"{field_name!r} exceeds its maximum length.")
    if await get_user_by_username(db, normalized) is not None:
        raise ValueError(f"A user named {normalized!r} already exists.")
    fields = {**extra_fields, field_name: _identifier_value(normalized)}
    columns = User.__table__.columns
    if email and User.EMAIL_FIELD not in columns:
        raise ValueError("The user model has no email field.")
    if (
        User.EMAIL_FIELD in columns
        and field_name != User.EMAIL_FIELD
        and (email or User.EMAIL_FIELD not in fields)
    ):
        fields[User.EMAIL_FIELD] = normalize_email(email)
    if "full_name" in columns:
        fields["full_name"] = full_name.strip()
    elif full_name:
        raise ValueError("The user model has no full name field.")
    user = User(**fields, is_superuser=is_superuser, is_active=is_active)
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
        errors = validate_password(raw_password, user=user)
        if errors:
            raise PasswordValidationError(errors)
    user.password_hash = await to_thread(hash_password, raw_password)
    user.password_changed_at = utcnow()
    await db.execute(delete(PasswordResetToken).where(PasswordResetToken.user_id == user.id))
    await db.flush()
    return await sessions.revoke_user_sessions(db, user.id)


async def _authenticate_model(db: AsyncSession, username: str, password: str) -> User | None:
    """Return the user on success and perform a dummy hash for unknown users."""
    try:
        identifier = _identifier_value(username)
    except ValueError:
        await to_thread(verify_dummy, password)
        return None
    user = await db.scalar(
        select(User)
        .where(getattr(User, User.USERNAME_FIELD) == identifier)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    now = utcnow()
    if user is None:
        await to_thread(verify_dummy, password)
        logger.info("Failed login for unknown username %r", username)
        return None
    valid, updated_hash = await to_thread(verify_and_update, password, user.password_hash)
    if not valid:
        return None
    if updated_hash is not None:
        user.password_hash = updated_hash
    user.last_login_at = now
    await db.flush()
    return user


async def authenticate_with_backend(
    db: AsyncSession,
    username: str = "",
    password: str = "",
    *,
    request: Request | None = None,
    **credentials: Any,
) -> tuple[User | None, str | None]:
    """Try configured backends in order and return the successful backend path."""
    for path in settings.AUTHENTICATION_BACKENDS:
        backend = load_backend(path)
        user = await backend.authenticate(
            db, request=request, username=username, password=password, **credentials
        )
        if user is not None:
            return cast(User, user), path
    return None, None


async def authenticate(
    db: AsyncSession, username: str = "", password: str = "", **credentials: Any
) -> User | None:
    """Compatibility wrapper returning only the authenticated user."""
    user, _ = await authenticate_with_backend(db, username, password, **credentials)
    return user


async def get_user_for_session(db: AsyncSession, user_id: int, backend_path: str) -> User | None:
    """Resolve a session user with its configured backend, as recorded at login."""
    if backend_path not in settings.AUTHENTICATION_BACKENDS:
        return None
    user = await load_backend(backend_path).get_user(db, user_id)
    return cast(User, user) if user is not None and user.is_active else None


async def change_password(
    db: AsyncSession, user: User, current_password: str, new_password: str
) -> int:
    """Change a password after checking the current one; revokes other sessions."""
    await db.refresh(user, with_for_update=True)
    if not is_usable_password(user.password_hash) or not await to_thread(
        verify_password, current_password, user.password_hash
    ):
        raise PasswordValidationError(["Current password is incorrect."])
    return await set_password(db, user, new_password)
