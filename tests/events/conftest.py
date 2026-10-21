from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import obox_builder
import pytest
from obox_builder.conf import SETTINGS_MODULE_ENV, settings
from obox_builder.db import dispose, get_engine, get_sessionmaker
from obox_builder.db.base import ModelBase
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture
def events_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from obox_builder.contrib.events import options
    from obox_builder.core import events

    monkeypatch.setattr(events, "_subscriptions", {})
    monkeypatch.setattr(options, "_trackers", {})
    monkeypatch.setenv(SETTINGS_MODULE_ENV, "tests.support.events_settings")
    obox_builder.setup()
    if settings.DATABASE_URL.startswith("sqlite"):
        monkeypatch.setattr(
            settings.load(), "DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'events.db'}"
        )
    from tests.support.apps.warehouse.models import Contact, Item

    monkeypatch.setattr(Item, "__events__", {"relations": ["tags"]})
    monkeypatch.setattr(Contact, "__events__", True)


@pytest.fixture
async def db(events_env: None) -> AsyncIterator[AsyncSession]:
    async with get_engine().begin() as connection:
        await connection.run_sync(ModelBase.metadata.drop_all)
        await connection.run_sync(ModelBase.metadata.create_all)
    try:
        async with get_sessionmaker()() as session:
            yield session
            await session.rollback()
    finally:
        await dispose()
