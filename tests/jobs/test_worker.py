from __future__ import annotations

import asyncio
import os
from datetime import timedelta
from pathlib import Path
from typing import Any

import anyio
import pytest
import sqlalchemy as sa
from obox_builder.conf import settings
from obox_builder.contrib.jobs import enqueue, retry, revoke
from obox_builder.contrib.jobs.models import Task, TaskAttempt
from obox_builder.contrib.jobs.models import Worker as WorkerRecord
from obox_builder.contrib.jobs.worker import Worker, claim, execute, shutdown
from obox_builder.core.utils.time import utcnow
from obox_builder.db.session import session_scope
from sqlalchemy.ext.asyncio import AsyncSession

from tests.jobs import read


async def update(task_id: str, **values: Any) -> None:
    async with session_scope() as db:
        await db.execute(sa.update(Task).where(Task.id == task_id).values(**values))


async def wait_for_file(path: Path) -> None:
    with anyio.fail_after(15):
        while not path.exists():
            await asyncio.sleep(0.01)


async def test_idle_worker_heartbeats_and_marks_clean_shutdown(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.load(), "JOBS_WORKER_HEARTBEAT_SECONDS", 0.1)
    worker = Worker(queues=("idle",), concurrency=2)
    running = asyncio.create_task(worker.run())
    try:
        with anyio.fail_after(10):
            row = None
            while row is None:
                row = await read(
                    sa.select(WorkerRecord).where(WorkerRecord.hostname == worker.hostname)
                )
                await asyncio.sleep(0.01)
            assert row.stopped_at is None
            assert (row.pid, row.queues, row.concurrency) == (os.getpid(), ["idle"], 2)
            first = row.heartbeat_at
            while (await read(sa.select(WorkerRecord))).heartbeat_at <= first:
                await asyncio.sleep(0.01)
    finally:
        await shutdown(worker, running)
    assert (await read(sa.select(WorkerRecord))).stopped_at is not None


async def test_run_once_presence_is_offline_after_exit(db: AsyncSession) -> None:
    worker = Worker()
    assert not await worker.run_once()
    first = await read(sa.select(WorkerRecord))
    assert first.stopped_at is not None
    assert not await worker.run_once()
    assert await read(sa.select(sa.func.count()).select_from(WorkerRecord)) == 1


@pytest.fixture
def children(monkeypatch: pytest.MonkeyPatch) -> list[asyncio.subprocess.Process]:
    processes: list[asyncio.subprocess.Process] = []
    spawn = asyncio.create_subprocess_exec

    async def record(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await spawn(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", record)
    return processes


async def test_cpu_process_result_context_and_delay_api(db: AsyncSession) -> None:
    from tests.support.apps.jobsdemo.tasks import cpu

    row = await cpu.delay(db, 1000)
    await db.commit()
    assert await Worker().run_once()
    row = await read(sa.select(Task).where(Task.id == row.id))
    assert row.status == "SUCCESS"
    assert row.result == {
        "sum": sum(i * i for i in range(1000)),
        "pid": row.result["pid"],
        "id": row.id,
        "retries": 0,
        "worker": row.worker,
    }
    assert row.result["pid"] != os.getpid()
    attempt = await read(sa.select(TaskAttempt).where(TaskAttempt.task_id == row.id))
    assert (attempt.status, attempt.retries, attempt.worker) == ("SUCCESS", 0, row.worker)
    assert attempt.date_done is not None
    assert row.date_done is not None


@pytest.mark.parametrize(
    "name,expected", [("jobs.transient", "SUCCESS"), ("jobs.failure", "FAILURE")]
)
async def test_retry_budget_history_and_redacted_errors(
    db: AsyncSession, name: str, expected: str
) -> None:
    row = await enqueue(db, name)
    task_id = row.id
    await db.commit()
    await Worker().run_once()
    row = await read(sa.select(Task).where(Task.id == task_id))
    assert (row.status, row.retries, row.date_done) == ("RETRY", 1, None)
    assert "private-error-message" not in row.traceback
    await update(task_id, eta=utcnow() - timedelta(seconds=1))
    await Worker().run_once()
    row = await read(sa.select(Task).where(Task.id == task_id))
    assert (row.status, row.retries) == (expected, 1)
    assert await read(sa.select(sa.func.count()).select_from(TaskAttempt)) == 2
    if expected == "FAILURE":
        assert await retry(db, task_id)
        await db.commit()
        row = await read(sa.select(Task).where(Task.id == task_id))
        assert (row.status, row.retries, row.max_retries) == ("RETRY", 2, 3)


@pytest.mark.parametrize(
    "name,args,status",
    [
        ("jobs.async", ["ok"], "SUCCESS"),
        ("jobs.no_retry", [], "FAILURE"),
        ("jobs.invalid_result", [], "FAILURE"),
    ],
)
async def test_async_tasks_retry_filter_and_result_serialization(
    db: AsyncSession, name: str, args: list[Any], status: str
) -> None:
    row = await enqueue(db, name, args=args)
    await db.commit()
    await Worker().run_once()
    row = await read(sa.select(Task).where(Task.id == row.id))
    assert (row.status, row.retries) == (status, 0)
    if name == "jobs.async":
        assert row.result == "ok"


async def test_competing_consumers_and_queue_isolation(db: AsyncSession) -> None:
    for _ in range(4):
        await enqueue(db, "jobs.cpu", queue="cpu")
    await db.commit()
    assert not await Worker().run_once()
    await asyncio.gather(*(Worker(queues=("cpu",)).run_once() for _ in range(4)))
    assert (
        await read(sa.select(sa.func.count()).select_from(Task).where(Task.status == "SUCCESS"))
        == 4
    )
    assert await read(sa.select(sa.func.count()).select_from(TaskAttempt)) == 4


@pytest.mark.parametrize("exhausted", [False, True])
async def test_crash_recovery_fences_old_worker_and_bounds_retries(
    db: AsyncSession, exhausted: bool
) -> None:
    row = await enqueue(db, "jobs.cpu")
    await db.commit()
    original = await claim(("default",), "original")
    assert original is not None
    values: dict[str, Any] = {"locked_until": utcnow() - timedelta(seconds=1)}
    if exhausted:
        values["max_retries"] = 0
    await update(row.id, **values)
    replacement = await claim(("default",), "replacement")
    await execute(original)
    if exhausted:
        assert replacement is None
        assert (await read(sa.select(Task))).status == "FAILURE"
    else:
        assert replacement is not None
        assert replacement.lock_token != original.lock_token
        await execute(replacement)
        result = await read(sa.select(Task))
        assert (result.status, result.retries) == ("SUCCESS", 1)
    assert (
        await read(sa.select(TaskAttempt).where(TaskAttempt.retries == 0))
    ).status == "ABANDONED"


async def test_hard_timeout_kills_cpu_process(
    db: AsyncSession, tmp_path: Path, children: list[asyncio.subprocess.Process]
) -> None:
    await enqueue(db, "jobs.timeout", args=[str(tmp_path / "timeout.pid")])
    await db.commit()
    await Worker().run_once()
    row = await read(sa.select(Task))
    assert (row.status, row.traceback) == ("FAILURE", "TimeoutError")
    assert len(children) == 1
    assert children[0].returncode is not None


@pytest.mark.parametrize("cancel", [False, True])
async def test_heartbeat_and_running_revoke(
    db: AsyncSession, tmp_path: Path, children: list[asyncio.subprocess.Process], cancel: bool
) -> None:
    marker, gate = tmp_path / "started", tmp_path / "release"
    row = await enqueue(db, "jobs.gated", args=[str(marker), str(gate)])
    await db.commit()
    active = asyncio.create_task(Worker().run_once())
    try:
        await wait_for_file(marker)
        initial = (await read(sa.select(Task))).locked_until
        if cancel:
            assert await revoke(db, row.id)
            await db.commit()
        else:
            with anyio.fail_after(10):
                while (await read(sa.select(Task))).locked_until <= initial:
                    await asyncio.sleep(0.05)
            gate.write_text("release", encoding="utf-8")
        await asyncio.wait_for(active, timeout=10)
        assert (await read(sa.select(Task))).status == ("REVOKED" if cancel else "SUCCESS")
        assert children[0].returncode is not None
    finally:
        gate.write_text("release", encoding="utf-8")
        if not active.done():
            active.cancel()
            await asyncio.gather(active, return_exceptions=True)


@pytest.mark.parametrize("force", [False, True])
async def test_shutdown_drains_or_kills_active_process(
    db: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    children: list[asyncio.subprocess.Process],
    force: bool,
) -> None:
    marker, gate = tmp_path / "started", tmp_path / "release"
    await enqueue(db, "jobs.gated", args=[str(marker), str(gate)])
    await db.commit()
    worker = Worker(concurrency=2)
    running = asyncio.create_task(worker.run())
    try:
        await wait_for_file(marker)
        if force:
            monkeypatch.setattr(settings.load(), "JOBS_SHUTDOWN_TIMEOUT", 0.05)
        else:
            gate.write_text("release", encoding="utf-8")
        await shutdown(worker, running)
        assert children[0].returncode is not None
        assert (await read(sa.select(Task))).status == ("STARTED" if force else "SUCCESS")
    finally:
        gate.write_text("release", encoding="utf-8")
        if not running.done():
            await shutdown(worker, running)
