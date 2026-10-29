"""Transaction-owned enqueue, retry and revoke operations; never commit."""

from __future__ import annotations

import inspect
import json
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from obox_builder.apps import apps
from obox_builder.conf import settings
from obox_builder.contrib.jobs.models import Task, TaskAttempt
from obox_builder.contrib.jobs.registry import NAME_RE, encode_json, get_task
from obox_builder.core.exceptions import ImproperlyConfigured
from obox_builder.core.utils.time import utcnow


def require_enabled() -> None:
    if not (apps.models_ready and apps.is_installed("obox_builder.contrib.jobs")):
        raise ImproperlyConfigured("Install contrib.jobs before using background tasks.")
    if not settings.JOBS_ENABLED:
        raise ImproperlyConfigured("Background tasks are disabled by JOBS_ENABLED.")


async def enqueue(
    db: AsyncSession,
    name: str,
    *,
    args: Sequence[Any] = (),
    kwargs: dict[str, Any] | None = None,
    eta: datetime | None = None,
    queue: str | None = None,
    dedup_key: str | None = None,
) -> Task:
    require_enabled()
    definition = get_task(name)
    queue = definition.queue if queue is None else queue
    if not NAME_RE.fullmatch(queue):
        raise ValueError("Queue names must be 1–155 safe characters.")
    if dedup_key is not None and (not dedup_key or len(dedup_key) > 255):
        raise ValueError("dedup_key must contain 1–255 characters.")
    if eta is not None and (eta.tzinfo is None or eta.utcoffset() is None):
        raise ValueError("eta must be timezone-aware.")
    if not isinstance(args, (list, tuple)) or (kwargs is not None and not isinstance(kwargs, dict)):
        raise ValueError("Use a list/tuple of args and a dictionary of kwargs.")
    kwargs = {} if kwargs is None else kwargs
    if any(not isinstance(key, str) for key in kwargs):
        raise ValueError("Keyword argument names must be strings.")
    inspect.signature(definition.func).bind(*args, **kwargs)
    arguments = json.loads(encode_json([list(args), kwargs]))
    insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
    row = (
        await db.scalars(
            insert(Task)
            .values(
                id=str(uuid.uuid4()),
                name=name,
                queue=queue,
                args=arguments[0],
                kwargs=arguments[1],
                max_retries=definition.max_retries,
                eta=eta.astimezone(timezone.utc) if eta is not None else utcnow(),
                dedup_key=dedup_key,
            )
            .on_conflict_do_nothing(index_elements=["dedup_key"])
            .returning(Task)
        )
    ).one_or_none()
    if row is None:
        row = (await db.scalars(sa.select(Task).where(Task.dedup_key == dedup_key))).one()
        if (row.name, row.queue, row.args, row.kwargs) != (name, queue, arguments[0], arguments[1]):
            raise ValueError("dedup_key already belongs to a different task or arguments.")
    return row


async def retry(db: AsyncSession, task_id: str) -> bool:
    """Requeue FAILURE/REVOKED tasks with a fresh retry budget and the same id."""
    require_enabled()
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
    require_enabled()
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
