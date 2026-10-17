"""Session creation, lookup, sliding renewal and revocation (DB-backed)."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Any, cast

from fastapi import Response
from sqlalchemy import delete, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from obox_builder.conf import settings
from obox_builder.contrib.auth.models import Session as AuthSession
from obox_builder.core.utils.time import ensure_aware, utcnow

TOKEN_BYTES = 32


def generate_token() -> str:
    """A 256-bit URL-safe token; the database only ever stores its SHA-256."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def session_expiry(*, created_at: datetime | None = None) -> datetime:
    now = utcnow()
    expiry = now + timedelta(seconds=settings.SESSION_COOKIE_AGE)
    if created_at is not None:
        cap = ensure_aware(created_at) + timedelta(seconds=settings.SESSION_ABSOLUTE_AGE)
        expiry = min(expiry, cap)
    return expiry


async def create_session(
    db: AsyncSession,
    user_id: int,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> tuple[str, AuthSession]:
    """Create a session row; returns ``(token, row)`` (only the hash is stored)."""
    token = generate_token()
    now = utcnow()
    row = AuthSession(
        token_hash=hash_token(token),
        user_id=user_id,
        data={},
        expires_at=session_expiry(),
        last_seen_at=now,
        ip=ip,
        user_agent=(user_agent or "")[:255] or None,
    )
    db.add(row)
    await db.flush()
    return token, row


def is_expired(row: AuthSession) -> bool:
    now = utcnow()
    if ensure_aware(row.expires_at) <= now:
        return True
    absolute = ensure_aware(row.created_at) + timedelta(seconds=settings.SESSION_ABSOLUTE_AGE)
    return now >= absolute


async def get_session(db: AsyncSession, token: str) -> AuthSession | None:
    """Fetch a live session by raw token; expired rows are deleted."""
    row = await db.scalar(select(AuthSession).where(AuthSession.token_hash == hash_token(token)))
    if row is None:
        return None
    if is_expired(row):
        await db.delete(row)
        await db.flush()
        return None
    return row


async def touch_session(db: AsyncSession, row: AuthSession) -> bool:
    """Sliding renewal; returns True when the expiry was extended."""
    now = utcnow()
    row.last_seen_at = now
    half_window = timedelta(seconds=settings.SESSION_COOKIE_AGE / 2)
    if ensure_aware(row.expires_at) - now < half_window:
        row.expires_at = session_expiry(created_at=row.created_at)
        await db.flush()
        return True
    return False


async def delete_session(db: AsyncSession, row: AuthSession) -> None:
    await db.delete(row)
    await db.flush()


async def delete_session_by_token(db: AsyncSession, token: str) -> None:
    await db.execute(delete(AuthSession).where(AuthSession.token_hash == hash_token(token)))
    await db.flush()


async def revoke_user_sessions(db: AsyncSession, user_id: int) -> int:
    """Delete every session of a user; used on password change/deactivation."""
    result = await db.execute(delete(AuthSession).where(AuthSession.user_id == user_id))
    await db.flush()
    return cast("CursorResult[Any]", result).rowcount or 0


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        settings.SESSION_COOKIE_NAME,
        token,
        max_age=settings.SESSION_COOKIE_AGE,
        httponly=True,
        samesite="lax",
        secure=settings.SESSION_COOKIE_SECURE and not settings.DEBUG,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        settings.SESSION_COOKIE_NAME,
        httponly=True,
        samesite="lax",
        secure=settings.SESSION_COOKIE_SECURE and not settings.DEBUG,
        path="/",
    )
