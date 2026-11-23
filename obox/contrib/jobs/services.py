"""Transaction-owned job scheduling operations; never commit."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from obox.contrib.jobs.models import Task, TaskAttempt, utcnow
from obox.contrib.jobs.queue import enqueue as enqueue_definition
from obox.contrib.jobs.registry import get_task


async def enqueue(
    db: AsyncSession,
    name: str,
    *,
    args: Sequence[Any] = (),
    kwargs: dict[str, Any] | None = None,
    eta: datetime | None = None,
    expires: datetime | None = None,
    priority: int | None = None,
    queue: str | None = None,
    dedup_key: str | None = None,
) -> Task:
    return await enqueue_definition(
        db,
        get_task(name),
        args=args,
        kwargs=kwargs,
        eta=eta,
        expires=expires,
        priority=priority,
        queue=queue,
        dedup_key=dedup_key,
    )


async def retry(db: AsyncSession, task_id: str) -> bool:
    """Requeue FAILURE/REVOKED tasks with a fresh retry budget and the same id."""
    row = (await db.scalars(sa.select(Task).where(Task.id == task_id))).one_or_none()
    if row is None:
        return False
    definition = get_task(row.name)
    updated = await db.scalar(
        sa.update(Task)
        .where(Task.id == task_id, Task.status.in_(("FAILURE", "REVOKED")))
        .values(
            status="RETRY",
            retries=Task.retries + 1,
            max_retries=Task.retries + 1 + definition.max_retries,
            eta=utcnow(),
            date_done=None,
            traceback=None,
            result=None,
            worker=None,
        )
        .returning(Task.id)
        .execution_options(synchronize_session=False)
    )
    return updated is not None


async def revoke(db: AsyncSession, task_id: str) -> bool:
    """Cancel queued work; a running child stops when its next lease renewal fails."""
    now = utcnow()
    updated = await db.scalar(
        sa.update(Task)
        .where(Task.id == task_id, Task.status.in_(("PENDING", "RETRY", "STARTED")))
        .values(status="REVOKED", date_done=now, lock_token=None, locked_until=None)
        .returning(Task.id)
        .execution_options(synchronize_session=False)
    )
    if updated is not None:
        await db.execute(
            sa.update(TaskAttempt)
            .where(TaskAttempt.task_id == task_id, TaskAttempt.status == "STARTED")
            .values(status="REVOKED", date_done=now)
        )
    return updated is not None
