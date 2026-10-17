from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from obox_builder.conf import SETTINGS_MODULE_ENV
from obox_builder.db.base import ModelBase
from sqlalchemy.ext.asyncio import AsyncSession

# Model classes require a ready app registry at import time (Django-style), and
# test modules import them at module level. Populate once here, then reset the
# global state so per-test fixtures still create it from scratch.
os.environ.setdefault(SETTINGS_MODULE_ENV, "tests.support.auth_settings")

import obox_builder as _obox_builder  # noqa: E402
from obox_builder.conf import settings as _settings  # noqa: E402
from obox_builder.core.signals import post_migrate as _post_migrate  # noqa: E402

_obox_builder.setup()
_settings.reset()
_obox_builder._reset()
_post_migrate.clear()  # drop receivers connected during the bootstrap populate


@pytest.fixture
def auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import obox_builder

    monkeypatch.setenv(SETTINGS_MODULE_ENV, "tests.support.auth_settings")
    obox_builder.setup()


@pytest.fixture
async def db(auth_env: None) -> AsyncIterator[AsyncSession]:
    """A session against a freshly created in-memory database."""
    from obox_builder.db import get_engine, get_sessionmaker

    async with get_engine().begin() as connection:
        await connection.run_sync(ModelBase.metadata.drop_all)
        await connection.run_sync(ModelBase.metadata.create_all)
    async with get_sessionmaker()() as session:
        yield session
        await session.rollback()
