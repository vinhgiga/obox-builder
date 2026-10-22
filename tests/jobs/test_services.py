from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from obox_builder.conf import settings
from obox_builder.contrib.jobs import current_task, enqueue, revoke, task
from obox_builder.contrib.jobs.models import Task
from obox_builder.contrib.jobs.registry import get_task, registered_tasks
from obox_builder.contrib.jobs.worker import Worker
from obox_builder.core.exceptions import ImproperlyConfigured
from obox_builder.core.utils.time import utcnow
from sqlalchemy.ext.asyncio import AsyncSession

from tests.jobs import read


async def test_enqueue_and_savepoints_follow_caller_transaction(db: AsyncSession) -> None:
    row = await enqueue(db, "jobs.cpu")
    task_id = row.task_id
    await db.rollback()
    assert await read(sa.select(Task).where(Task.task_id == task_id)) is None
    async with db.begin_nested():
        row = await enqueue(db, "jobs.cpu")
        task_id = row.task_id
    await db.rollback()
    assert await read(sa.select(Task).where(Task.task_id == task_id)) is None
    row = await enqueue(db, "jobs.cpu")
    await db.commit()
    assert (await read(sa.select(Task).where(Task.task_id == row.task_id))).status == "PENDING"


async def test_rolled_back_savepoint_preserves_other_jobs(db: AsyncSession) -> None:
    kept = await enqueue(db, "jobs.cpu")
    nested = await db.begin_nested()
    removed = await enqueue(db, "jobs.cpu")
    removed_id = removed.task_id
    await nested.rollback()
    await db.commit()
    assert await read(sa.select(Task).where(Task.task_id == kept.task_id)) is not None
    assert await read(sa.select(Task).where(Task.task_id == removed_id)) is None


async def test_dedup_returns_original_and_rejects_different_arguments(db: AsyncSession) -> None:
    original = await enqueue(db, "jobs.cpu", args=[10], dedup_key="same")
    duplicate = await enqueue(db, "jobs.cpu", args=[10], dedup_key="same")
    assert duplicate.task_id == original.task_id
    with pytest.raises(ValueError, match="different"):
        await enqueue(db, "jobs.cpu", args=[11], dedup_key="same")
    await db.commit()
    assert await read(sa.select(sa.func.count()).select_from(Task)) == 1


@pytest.mark.parametrize(
    "options",
    [
        {"name": "missing"},
        {"queue": "bad queue"},
        {"dedup_key": ""},
        {"dedup_key": "x" * 256},
        {"eta": utcnow().replace(tzinfo=None)},
        {"args": "invalid"},
        {"kwargs": []},
        {"kwargs": {1: "invalid"}},
        {"args": [float("nan")]},
        {"args": [object()]},
        {"kwargs": {"unknown": 1}},
    ],
)
async def test_invalid_enqueue_is_not_persisted(db: AsyncSession, options: dict[str, Any]) -> None:
    with pytest.raises((TypeError, ValueError)):
        await enqueue(db, **{"name": "jobs.cpu", **options})
    await db.commit()
    assert await read(sa.select(sa.func.count()).select_from(Task)) == 0


async def test_snapshot_payload_and_limit(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = [100]
    row = await enqueue(db, "jobs.cpu", args=args)
    args[0] = 200
    await db.commit()
    assert (await read(sa.select(Task).where(Task.task_id == row.task_id))).args == [100]
    monkeypatch.setattr(settings.load(), "JOBS_MAX_PAYLOAD_BYTES", 4)
    with pytest.raises(ValueError, match="MAX_PAYLOAD"):
        await enqueue(db, "jobs.cpu")


async def test_eta_queue_and_revoking_queued_work(db: AsyncSession) -> None:
    row = await enqueue(db, "jobs.cpu", queue="scheduled", eta=utcnow() + timedelta(hours=1))
    await db.commit()
    assert not await Worker().run_once()
    assert not await Worker(queues=("scheduled",)).run_once()
    assert await revoke(db, row.task_id)
    await db.commit()
    assert not await revoke(db, row.task_id)
    assert (await read(sa.select(Task).where(Task.task_id == row.task_id))).status == "REVOKED"


@pytest.mark.parametrize(
    "options",
    [
        {"queue": "bad queue"},
        {"max_retries": -1},
        {"max_retries": True},
        {"time_limit": 0},
        {"time_limit": float("inf")},
        {"autoretry_for": (str,)},
    ],
)
def test_invalid_task_options(jobs_env: None, options: dict[str, Any]) -> None:
    with pytest.raises(ImproperlyConfigured):
        task(**options)


def test_registry_discovery_duplicate_and_context(jobs_env: None) -> None:
    assert "jobs.cpu" in {definition.name for definition in registered_tasks()}
    with pytest.raises(ImproperlyConfigured, match="Duplicate"):
        task(name="jobs.cpu")(test_registry_discovery_duplicate_and_context)
    with pytest.raises(LookupError):
        current_task()
    with pytest.raises(ValueError, match="Unknown task"):
        get_task("missing")


def test_local_tasks_are_rejected(jobs_env: None) -> None:
    def local() -> None:
        pass

    with pytest.raises(ImproperlyConfigured, match="module-level"):
        task(name="jobs.local")(local)


def test_disabled_feature_and_invalid_workers(
    jobs_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    invalid: list[dict[str, Any]] = [{"queues": ()}, {"concurrency": 0}, {"concurrency": True}]
    for options in invalid:
        with pytest.raises(ImproperlyConfigured):
            Worker(**options)
    monkeypatch.setattr(settings.load(), "JOBS_ENABLED", False)
    with pytest.raises(ImproperlyConfigured, match="disabled"):
        Worker()
