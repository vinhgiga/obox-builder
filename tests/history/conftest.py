from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable

import pytest
from obox_builder.conf import SETTINGS_MODULE_ENV
from obox_builder.db.base import ModelBase
from sqlalchemy.ext.asyncio import AsyncSession

# Model classes require a ready app registry at import time (Django-style), and
# test modules import the warehouse models at module level. Force our settings
# module for this bootstrap, then restore the environment so later conftests
# keep following the suite-wide convention (tests/support/admin_settings).
_previous_settings_module = os.environ.get(SETTINGS_MODULE_ENV)
os.environ[SETTINGS_MODULE_ENV] = "tests.support.history_settings"

import obox_builder as _obox_builder  # noqa: E402
from obox_builder.conf import settings as _settings  # noqa: E402
from obox_builder.core.signals import post_migrate as _post_migrate  # noqa: E402

_obox_builder.setup()
_settings.reset()
_obox_builder._reset()
_post_migrate.clear()

if _previous_settings_module is None:
    os.environ.pop(SETTINGS_MODULE_ENV, None)
else:
    os.environ[SETTINGS_MODULE_ENV] = _previous_settings_module


@pytest.fixture
def history_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import obox_builder

    monkeypatch.setenv(SETTINGS_MODULE_ENV, "tests.support.history_settings")
    obox_builder.setup()


@pytest.fixture
def override_settings(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Temporarily override attributes on the loaded settings object."""
    from obox_builder.conf import settings

    def apply(**values: object) -> None:
        assert settings._wrapped is not None
        for key, value in values.items():
            monkeypatch.setattr(settings._wrapped, key, value)

    return apply


@pytest.fixture
async def db(history_env: None) -> AsyncIterator[AsyncSession]:
    """A session against a freshly created in-memory database."""
    from obox_builder.db import dispose, get_engine, get_sessionmaker

    async with get_engine().begin() as connection:
        await connection.run_sync(ModelBase.metadata.drop_all)
        await connection.run_sync(ModelBase.metadata.create_all)
    async with get_sessionmaker()() as session:
        yield session
        await session.rollback()
    await dispose()
