from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest
from obox_builder.conf import settings
from obox_builder.contrib.auth import password_reset, services, sessions
from obox_builder.contrib.auth.models import PasswordResetToken
from obox_builder.contrib.auth.models import Session as AuthSession
from obox_builder.contrib.auth.validators import PasswordValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def test_password_reset_token_is_hashed_one_use_and_revokes_sessions(
    db: AsyncSession,
) -> None:
    from tests.support import email_backend

    email_backend.MESSAGES.clear()
    config = settings.load()
    settings.configure(
        config.model_copy(
            update={
                "EMAIL_BACKEND": "tests.support.email_backend.MemoryBackend",
                "PASSWORD_RESET_COOLDOWN": 0,
                "PASSWORD_RESET_BASE_URL": "https://admin.example.org",
            }
        )
    )
    user = await services.create_user(db, "alice", "s3cure-password", email="alice@example.org")
    await sessions.create_session(db, user.id)

    await password_reset.request_password_reset(db, " ALICE@example.org ")

    message = email_backend.MESSAGES[0]
    assert message.recipients == ("alice@example.org",)
    assert "https://admin.example.org/admin/password_reset/confirm?token=" in message.body
    token = parse_qs(urlsplit(message.body.splitlines()[2]).query)["token"][0]
    row = await db.scalar(select(PasswordResetToken))
    assert row is not None
    assert row.token_hash == password_reset.hash_reset_token(token)
    assert token not in row.token_hash
    assert await password_reset.is_valid_reset_token(db, token)

    with pytest.raises(PasswordValidationError):
        await password_reset.reset_password(db, token, "short")
    assert await password_reset.is_valid_reset_token(db, token)
    assert await password_reset.reset_password(db, token, "brand-new-secret")
    assert await services.authenticate(db, "alice", "brand-new-secret") is not None
    assert await services.authenticate(db, "alice", "s3cure-password") is None
    assert await db.scalar(select(AuthSession).where(AuthSession.user_id == user.id)) is None
    assert not await password_reset.is_valid_reset_token(db, token)
    assert not await password_reset.reset_password(db, token, "another-secret-123")


async def test_reset_request_is_quiet_for_unknown_addresses_and_rate_limited(
    db: AsyncSession,
) -> None:
    from tests.support import email_backend

    email_backend.MESSAGES.clear()
    config = settings.load()
    settings.configure(
        config.model_copy(
            update={
                "EMAIL_BACKEND": "tests.support.email_backend.MemoryBackend",
                "PASSWORD_RESET_COOLDOWN": 60,
            }
        )
    )
    user = await services.create_user(db, "alice", "s3cure-password", email="alice@example.org")

    await password_reset.request_password_reset(db, "missing@example.org")
    assert email_backend.MESSAGES == []
    await password_reset.request_password_reset(db, user.email)
    await password_reset.request_password_reset(db, user.email)
    assert len(email_backend.MESSAGES) == 1


async def test_expired_reset_token_cannot_change_password(db: AsyncSession) -> None:
    from datetime import timedelta

    from obox_builder.core.utils.time import utcnow

    user = await services.create_user(db, "alice", "s3cure-password")
    token = "expired-token"
    db.add(
        PasswordResetToken(
            user_id=user.id,
            token_hash=password_reset.hash_reset_token(token),
            expires_at=utcnow() - timedelta(seconds=1),
        )
    )

    assert not await password_reset.is_valid_reset_token(db, token)
    assert not await password_reset.reset_password(db, token, "brand-new-secret")


async def test_password_change_revokes_previously_issued_reset_token(db: AsyncSession) -> None:
    from datetime import timedelta

    from obox_builder.core.utils.time import utcnow

    user = await services.create_user(db, "alice", "s3cure-password")
    token = "existing-reset-token"
    db.add(
        PasswordResetToken(
            user_id=user.id,
            token_hash=password_reset.hash_reset_token(token),
            expires_at=utcnow() + timedelta(hours=1),
        )
    )
    await db.flush()
    await services.change_password(db, user, "s3cure-password", "brand-new-secret")
    assert not await password_reset.is_valid_reset_token(db, token)
    assert not await password_reset.reset_password(db, token, "another-secret-123")


async def test_reset_shared_email_only_sends_to_active_usable_accounts(db: AsyncSession) -> None:
    from tests.support import email_backend

    email_backend.MESSAGES.clear()
    config = settings.load()
    settings.configure(
        config.model_copy(update={"EMAIL_BACKEND": "tests.support.email_backend.MemoryBackend"})
    )
    await services.create_user(db, "alice", "s3cure-password", email="shared@example.org")
    await services.create_user(db, "bob", "s3cure-password", email="shared@example.org")
    await services.create_user(
        db, "inactive", "s3cure-password", email="shared@example.org", is_active=False
    )
    await services.create_user(db, "no-password", email="shared@example.org")
    await password_reset.request_password_reset(db, "shared@example.org")
    assert len(email_backend.MESSAGES) == 2
    tokens = [
        parse_qs(urlsplit(message.body.splitlines()[2]).query)["token"][0]
        for message in email_backend.MESSAGES
    ]
    assert tokens[0] != tokens[1]
    assert all([await password_reset.is_valid_reset_token(db, token) for token in tokens])
