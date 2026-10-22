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
def jobs_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from obox_builder.contrib.jobs import registry

    monkeypatch.setattr(registry, "_tasks", {})
    monkeypatch.setenv(SETTINGS_MODULE_ENV, "tests.support.jobs_settings")
    obox_builder.setup()
    if settings.DATABASE_URL.startswith("sqlite"):
        monkeypatch.setattr(
            settings.load(), "DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'jobs.db'}"
        )


@pytest.fixture
async def db(jobs_env: None) -> AsyncIterator[AsyncSession]:
    async with get_engine().begin() as connection:
        await connection.run_sync(ModelBase.metadata.drop_all)
        await connection.run_sync(ModelBase.metadata.create_all)
    try:
        async with get_sessionmaker()() as session:
            yield session
            await session.rollback()
    finally:
        await dispose()
