from __future__ import annotations

from datetime import timedelta

from obox_builder.conf import settings
from obox_builder.contrib.auth import sessions
from obox_builder.contrib.auth.models import Session as AuthSession
from obox_builder.contrib.auth.services import create_user
from obox_builder.core.utils.time import ensure_aware, utcnow
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def test_create_session_stores_only_the_hash(db: AsyncSession) -> None:
    user = await create_user(db, "erin", "s3cure-password")
    token, row = await sessions.create_session(db, user.id, ip="127.0.0.1", user_agent="pytest")
    assert token != row.token_hash
    assert row.token_hash == sessions.hash_token(token)
    assert len(row.token_hash) == 64
    assert ensure_aware(row.expires_at) > utcnow()
    assert row.ip == "127.0.0.1"
    assert row.user_agent == "pytest"


async def test_get_session_returns_live_session(db: AsyncSession) -> None:
    user = await create_user(db, "frank", "s3cure-password")
    token, row = await sessions.create_session(db, user.id)
    found = await sessions.get_session(db, token)
    assert found is not None
    assert found.id == row.id
    assert await sessions.get_session(db, "bogus-token") is None


async def test_expired_session_is_deleted_on_access(db: AsyncSession) -> None:
    user = await create_user(db, "gina", "s3cure-password")
    token, row = await sessions.create_session(db, user.id)
    row.expires_at = utcnow() - timedelta(seconds=1)
    await db.flush()
    assert await sessions.get_session(db, token) is None
    assert await db.scalar(select(AuthSession).where(AuthSession.id == row.id)) is None


async def test_absolute_age_caps_sliding_expiry(db: AsyncSession) -> None:
    user = await create_user(db, "hugo", "s3cure-password")
    token, row = await sessions.create_session(db, user.id)
    row.created_at = utcnow() - timedelta(seconds=settings.SESSION_ABSOLUTE_AGE + 10)
    row.expires_at = utcnow() + timedelta(hours=1)
    await db.flush()
    assert sessions.is_expired(row)
    assert await sessions.get_session(db, token) is None


async def test_touch_renews_only_in_the_second_half_of_the_window(db: AsyncSession) -> None:
    user = await create_user(db, "iris", "s3cure-password")
    _token, row = await sessions.create_session(db, user.id)
    assert not await sessions.touch_session(db, row)

    row.expires_at = utcnow() + timedelta(seconds=settings.SESSION_COOKIE_AGE / 4)
    await db.flush()
    assert await sessions.touch_session(db, row)
    assert ensure_aware(row.expires_at) > utcnow() + timedelta(
        seconds=settings.SESSION_COOKIE_AGE / 2
    )
    assert row.last_seen_at is not None


async def test_revoke_user_sessions_deletes_all_of_them(db: AsyncSession) -> None:
    alice = await create_user(db, "alice2", "s3cure-password")
    bob = await create_user(db, "bob2", "s3cure-password")
    await sessions.create_session(db, alice.id)
    await sessions.create_session(db, alice.id)
    await sessions.create_session(db, bob.id)
    revoked = await sessions.revoke_user_sessions(db, alice.id)
    assert revoked == 2
    assert await db.scalar(select(AuthSession).where(AuthSession.user_id == bob.id)) is not None


async def test_cookie_helpers_set_and_clear(auth_env: None) -> None:
    from fastapi import Response

    response = Response()
    sessions.set_session_cookie(response, "token-value")
    header = response.headers["set-cookie"]
    assert settings.SESSION_COOKIE_NAME in header
    assert "HttpOnly" in header
    assert "SameSite=lax" in header.replace("samesite", "SameSite")

    cleared = Response()
    sessions.clear_session_cookie(cleared)
    assert (
        "Max-Age=0" in cleared.headers["set-cookie"]
        or "max-age=0" in cleared.headers["set-cookie"].lower()
    )
