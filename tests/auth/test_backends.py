from __future__ import annotations

from obox_builder.conf import settings
from obox_builder.contrib.auth import services, sessions
from sqlalchemy.ext.asyncio import AsyncSession

BACKEND = "tests.support.auth_backend.PasswordBackend"


async def test_custom_backend_authentication_is_recorded_in_session(db: AsyncSession) -> None:
    config = settings.load()
    settings.configure(config.model_copy(update={"AUTHENTICATION_BACKENDS": [BACKEND]}))
    user = await services.create_user(db, "alice", "s3cure-password")

    found, backend = await services.authenticate_with_backend(db, "alice", "backend-secret")

    assert found is not None
    assert found.id == user.id
    assert backend == BACKEND
    token, row = await sessions.create_session(db, user.id, backend=backend)
    assert token
    assert row.data["backend"] == BACKEND
    assert await services.get_user_for_session(db, user.id, BACKEND) == user

    settings.configure(config.model_copy(update={"AUTHENTICATION_BACKENDS": []}))
    assert await services.get_user_for_session(db, user.id, BACKEND) is None


async def test_default_model_backend_remains_compatible(db: AsyncSession) -> None:
    user = await services.create_user(db, "alice", "s3cure-password")
    found, backend = await services.authenticate_with_backend(db, "alice", "s3cure-password")

    assert found is not None
    assert found.id == user.id
    assert backend == "obox_builder.contrib.auth.backends.ModelBackend"
    assert await services.authenticate(db, "alice", "s3cure-password") == user
