import argparse
import asyncio
import sys
from types import ModuleType
from typing import Any

import pytest
import sqlalchemy as sa
from obox.conf import settings
from obox.contrib.jobs import cli
from obox.db.engine import _factory
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.parametrize("failure", [False, True])
def test_commands_use_standard_database_settings_and_close_resources(
    failure: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        module = ModuleType("job_settings")
        module.DATABASES = {"default": {"ENGINE": "obox.db.backends.sqlite3", "NAME": ":memory:"}}
        monkeypatch.setitem(sys.modules, "job_settings", module)
        monkeypatch.setenv("OBOX_SETTINGS_MODULE", "job_settings")
        monkeypatch.setattr(settings, "_wrapped", None)
        factory = _factory()
        disposed = []
        sa.event.listen(
            factory.kw["bind"].sync_engine, "engine_disposed", lambda _: disposed.append(True)
        )

        async def execute(arguments: Any, sessions: async_sessionmaker[AsyncSession]) -> None:
            assert sessions is factory
            async with sessions() as session:
                assert await session.scalar(sa.text("select 1")) == 1
            if failure:
                raise RuntimeError("command failed")

        monkeypatch.setattr(cli, "_run", execute)
        if failure:
            with pytest.raises(RuntimeError, match="command failed"):
                await cli.run(argparse.Namespace(settings="job_settings"))
        else:
            await cli.run(argparse.Namespace(settings="job_settings"))
        assert disposed == [True]

    asyncio.run(scenario())
