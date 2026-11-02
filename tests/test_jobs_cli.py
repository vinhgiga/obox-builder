import argparse
import asyncio
import sys
from types import ModuleType
from typing import Any

import pytest
import sqlalchemy as sa
from obox_builder.contrib.jobs import cli
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


@pytest.mark.parametrize("borrowed", [False, True])
def test_commands_share_database_lifecycle(borrowed: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite://") if borrowed else None
        module = ModuleType("job_settings")
        if engine is None:
            module.DATABASE_URL = "sqlite+aiosqlite://"  # type: ignore[attr-defined]
        else:
            module.SESSION_FACTORY = async_sessionmaker(engine)  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "job_settings", module)
        monkeypatch.setenv("OBOX_SETTINGS_MODULE", "job_settings")
        factories = []
        disposed = []
        if engine is not None:
            sa.event.listen(engine.sync_engine, "engine_disposed", lambda _: disposed.append(True))

        async def execute(arguments: Any, sessions: async_sessionmaker[AsyncSession]) -> None:
            factories.append(sessions)
            async with sessions() as session:
                assert await session.scalar(sa.text("select 1")) == 1
            raise RuntimeError("command failed")

        monkeypatch.setattr(cli, "_run", execute)
        try:
            with pytest.raises(RuntimeError, match="command failed"):
                await cli.run(argparse.Namespace(settings="job_settings"))
            assert disposed == []
            if borrowed:
                assert factories[0] is module.SESSION_FACTORY
            else:
                assert factories[0].kw["bind"] is None
        finally:
            if engine is not None:
                await engine.dispose()

    asyncio.run(scenario())
