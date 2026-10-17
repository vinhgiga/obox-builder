from __future__ import annotations

from datetime import timedelta

import pytest
from obox_builder.conf import settings
from obox_builder.contrib.auth import services
from obox_builder.contrib.auth.models import Session as AuthSession
from obox_builder.contrib.auth.models import User
from obox_builder.contrib.auth.validators import PasswordValidationError
from obox_builder.core.utils.time import ensure_aware, utcnow
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def test_create_user_normalizes_username_and_email(db: AsyncSession) -> None:
    user = await services.create_user(
        db, "  Alice  ", "s3cure-password", email="Alice@Example.COM", full_name=" Alice Doe "
    )
    assert user.username == "alice"
    assert user.email == "alice@example.com"
    assert user.full_name == "Alice Doe"
    assert user.password_hash.startswith("$argon2")
    assert user.password_changed_at is not None
    assert user.is_active
    assert not user.is_superuser


async def test_create_user_rejects_duplicates_and_empty(db: AsyncSession) -> None:
    await services.create_user(db, "alice", "s3cure-password")
    for name in ("alice", "ALICE", ""):
        with pytest.raises(ValueError, match="exists|empty"):
            await services.create_user(db, name, "s3cure-password")


async def test_create_user_validates_password(db: AsyncSession) -> None:
    with pytest.raises(PasswordValidationError) as exc_info:
        await services.create_user(db, "alice", "short")
    assert exc_info.value.errors


async def test_authenticate_success_resets_counters(db: AsyncSession) -> None:
    user = await services.create_user(db, "alice", "s3cure-password")
    user.failed_login_count = 2
    await db.flush()
    found = await services.authenticate(db, "ALICE", "s3cure-password")
    assert found is not None
    assert found.id == user.id
    assert found.failed_login_count == 0
    assert found.locked_until is None
    assert found.last_login_at is not None


async def test_authenticate_wrong_password_increments_and_locks(db: AsyncSession) -> None:
    await services.create_user(db, "alice", "s3cure-password")
    for _ in range(settings.LOGIN_MAX_ATTEMPTS):
        assert await services.authenticate(db, "alice", "wrong-password") is None
    locked = await db.scalar(select(User).where(User.username == "alice"))
    assert locked is not None
    assert locked.locked_until is not None
    assert ensure_aware(locked.locked_until) > utcnow()
    assert locked.failed_login_count == 0
    assert await services.authenticate(db, "alice", "s3cure-password") is None


async def test_lock_expires_and_login_works_again(db: AsyncSession) -> None:
    await services.create_user(db, "alice", "s3cure-password")
    for _ in range(settings.LOGIN_MAX_ATTEMPTS):
        await services.authenticate(db, "alice", "wrong-password")
    user = await db.scalar(select(User).where(User.username == "alice"))
    assert user is not None
    user.locked_until = utcnow() - timedelta(seconds=1)
    await db.flush()
    found = await services.authenticate(db, "alice", "s3cure-password")
    assert found is not None
    assert found.locked_until is None


async def test_authenticate_unknown_user_returns_none(db: AsyncSession) -> None:
    assert await services.authenticate(db, "nobody", "whatever-password") is None


async def test_set_password_revokes_sessions(db: AsyncSession) -> None:
    user = await services.create_user(db, "alice", "s3cure-password")
    from obox_builder.contrib.auth import sessions

    await sessions.create_session(db, user.id)
    await sessions.create_session(db, user.id)
    revoked = await services.set_password(db, user, "even-more-s3cure")
    assert revoked == 2
    assert await db.scalar(select(AuthSession).where(AuthSession.user_id == user.id)) is None
    assert await services.authenticate(db, "alice", "even-more-s3cure") is not None


async def test_change_password_requires_current_password(db: AsyncSession) -> None:
    user = await services.create_user(db, "alice", "s3cure-password")
    with pytest.raises(PasswordValidationError, match="Current password is incorrect"):
        await services.change_password(db, user, "wrong-password", "brand-new-secret")
    revoked = await services.change_password(db, user, "s3cure-password", "brand-new-secret")
    assert revoked >= 0
    assert await services.authenticate(db, "alice", "brand-new-secret") is not None
