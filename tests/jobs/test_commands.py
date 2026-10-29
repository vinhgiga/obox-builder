from __future__ import annotations

from datetime import timedelta
from types import ModuleType

import pytest
import sqlalchemy as sa
from obox_builder.contrib.jobs.management.commands import (
    jobs_enqueue,
    jobs_purge,
    jobs_retry,
    jobs_revoke,
    jobs_status,
    jobs_worker,
)
from obox_builder.contrib.jobs.models import Task, TaskAttempt
from obox_builder.core.management.base import CommandError
from obox_builder.core.utils.time import utcnow
from obox_builder.db.session import session_scope
from sqlalchemy.ext.asyncio import AsyncSession

from tests.jobs import read


async def test_cli_lifecycle_purge_and_cascade(
    db: AsyncSession, capsys: pytest.CaptureFixture[str]
) -> None:
    await jobs_enqueue.handle.func(
        name="jobs.cpu", args="[100]", kwargs="{}", eta=None, queue=None, dedup_key=None
    )
    row = await read(sa.select(Task))
    await jobs_revoke.handle.func(row.id)
    await jobs_retry.handle.func(row.id)
    await jobs_worker.handle.func(queue=["default"], concurrency=1, once=True)
    await jobs_status.handle.func()
    assert (await read(sa.select(Task))).status == "SUCCESS"
    async with session_scope() as session:
        await session.execute(sa.update(Task).values(date_done=utcnow() - timedelta(days=8)))
    await jobs_purge.handle.func(days=7, batch_size=1000)
    assert await read(sa.select(sa.func.count()).select_from(Task)) == 0
    assert await read(sa.select(sa.func.count()).select_from(TaskAttempt)) == 0
    output = capsys.readouterr().out
    assert "SUCCESS = 1" in output
    assert "Purged 1" in output


@pytest.mark.parametrize("args", ["not-json", "{}", "null"])
async def test_enqueue_command_rejects_invalid_arguments(db: AsyncSession, args: str) -> None:
    with pytest.raises(CommandError):
        await jobs_enqueue.handle.func(
            name="jobs.cpu", args=args, kwargs="{}", eta=None, queue=None, dedup_key=None
        )


@pytest.mark.parametrize("module", [jobs_retry, jobs_revoke])
async def test_commands_report_missing_task(db: AsyncSession, module: ModuleType) -> None:
    with pytest.raises(CommandError, match="does not exist"):
        await module.handle.func("missing")
