"""Password reset tokens and email delivery."""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import timedelta
from typing import Any, cast
from urllib.parse import quote

from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from obox_builder.conf import settings
from obox_builder.contrib.auth import services
from obox_builder.contrib.auth.hashers import is_usable_password
from obox_builder.contrib.auth.models import PasswordResetToken, User
from obox_builder.contrib.auth.validators import PasswordValidationError, validate_password
from obox_builder.contrib.email import send_mail
from obox_builder.core.utils.time import ensure_aware, utcnow

logger = logging.getLogger(__name__)


def hash_reset_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def request_password_reset(db: AsyncSession, email: str) -> None:
    """Email a reset link when an active account exists; otherwise return quietly."""
    normalized = services.normalize_email(email)
    if not normalized:
        return
    users = await db.scalars(select(User).where(func.lower(User.email) == normalized))
    for user in users:
        if user.is_active and is_usable_password(user.password_hash):
            await _send_reset(db, user)


async def _send_reset(db: AsyncSession, user: User) -> None:
    now = utcnow()
    recent = await db.scalar(
        select(PasswordResetToken)
        .where(PasswordResetToken.user_id == user.id)
        .order_by(PasswordResetToken.created_at.desc())
        .limit(1)
    )
    if (
        recent is not None
        and settings.PASSWORD_RESET_COOLDOWN
        and now - ensure_aware(recent.created_at)
        < timedelta(seconds=settings.PASSWORD_RESET_COOLDOWN)
    ):
        return

    await db.execute(delete(PasswordResetToken).where(PasswordResetToken.user_id == user.id))
    token = secrets.token_urlsafe(32)
    reset = PasswordResetToken(
        user_id=user.id,
        token_hash=hash_reset_token(token),
        expires_at=now + timedelta(seconds=settings.PASSWORD_RESET_TIMEOUT),
    )
    db.add(reset)
    await db.flush()
    link = (
        f"{settings.PASSWORD_RESET_BASE_URL}/admin/password_reset/confirm"
        f"?token={quote(token, safe='')}"
    )
    try:
        sent = await send_mail(
            "Reset your password",
            "Use this one-time link to choose a new password:\n\n"
            f"{link}\n\n"
            f"The link expires in {settings.PASSWORD_RESET_TIMEOUT // 60} minutes. "
            "If you did not request this change, you can ignore this email.",
            user.email,
        )
        if sent != 1:
            raise RuntimeError("The configured email backend did not deliver the message.")
    except Exception:
        await db.delete(reset)
        await db.flush()
        logger.exception("Password reset email delivery failed.")
        raise


async def is_valid_reset_token(db: AsyncSession, token: str) -> bool:
    if not token:
        return False
    now = utcnow()
    row = await db.scalar(
        select(PasswordResetToken).where(
            PasswordResetToken.token_hash == hash_reset_token(token),
            PasswordResetToken.used_at.is_(None),
            PasswordResetToken.expires_at > now,
        )
    )
    if row is None:
        return False
    user = await db.get(User, row.user_id)
    return user is not None and user.is_active


async def reset_password(db: AsyncSession, token: str, new_password: str) -> bool:
    """Atomically consume a valid token, update the password and revoke sessions."""
    if not token:
        return False
    now = utcnow()
    row = await db.scalar(
        select(PasswordResetToken).where(
            PasswordResetToken.token_hash == hash_reset_token(token),
            PasswordResetToken.used_at.is_(None),
            PasswordResetToken.expires_at > now,
        )
    )
    if row is None:
        return False
    user = await db.get(User, row.user_id)
    if user is None or not user.is_active:
        return False
    errors = validate_password(new_password, user=user)
    if errors:
        raise PasswordValidationError(errors)

    result = await db.execute(
        update(PasswordResetToken)
        .where(
            PasswordResetToken.id == row.id,
            PasswordResetToken.used_at.is_(None),
            PasswordResetToken.expires_at > now,
        )
        .values(used_at=now)
        .execution_options(synchronize_session=False)
    )
    if cast("CursorResult[Any]", result).rowcount != 1:
        return False
    row.used_at = now
    await services.set_password(db, user, new_password)
    return True
