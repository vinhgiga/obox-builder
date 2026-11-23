"""Database insertion for registered job definitions."""

from __future__ import annotations

import inspect
import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from obox.contrib.jobs.contracts import NAME_RE, encode_json
from obox.contrib.jobs.models import Task, utcnow


async def enqueue(
    db: AsyncSession,
    definition: Any,
    *,
    args: Sequence[Any] = (),
    kwargs: dict[str, Any] | None = None,
    eta: datetime | None = None,
    expires: datetime | None = None,
    priority: int | None = None,
    queue: str | None = None,
    dedup_key: str | None = None,
) -> Task:
    name = definition.name
    queue_name = definition.queue if queue is None else queue
    expires = definition.expires if expires is None else expires
    priority = definition.priority if priority is None else priority
    priority = 0 if priority is None else priority
    if not NAME_RE.fullmatch(queue_name):
        raise ValueError("Queue names must be 1-155 safe characters.")
    if dedup_key is not None and (not dedup_key or len(dedup_key) > 255):
        raise ValueError("dedup_key must contain 1-255 characters.")
    if eta is not None and (eta.tzinfo is None or eta.utcoffset() is None):
        raise ValueError("eta must be timezone-aware.")
    if expires is not None and (expires.tzinfo is None or expires.utcoffset() is None):
        raise ValueError("expires must be timezone-aware.")
    expires = expires.astimezone(UTC) if expires is not None else None
    if isinstance(priority, bool) or not isinstance(priority, int) or not 0 <= priority <= 255:
        raise ValueError("priority must be an integer from 0 to 255.")
    if not isinstance(args, (list, tuple)) or (kwargs is not None and not isinstance(kwargs, dict)):
        raise ValueError("Use a list/tuple of args and a dictionary of kwargs.")
    kwargs = {} if kwargs is None else kwargs
    if any(not isinstance(key, str) for key in kwargs):
        raise ValueError("Keyword argument names must be strings.")
    inspect.signature(definition.func).bind(*args, **kwargs)
    arguments = json.loads(encode_json([list(args), kwargs]))
    if db.get_bind().dialect.name not in {"sqlite", "postgresql"}:
        raise ValueError("Background jobs require SQLite or PostgreSQL.")
    insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
    row = (
        await db.scalars(
            insert(Task)
            .values(
                id=str(uuid.uuid4()),
                name=name,
                queue=queue_name,
                args=arguments[0],
                kwargs=arguments[1],
                max_retries=definition.max_retries,
                eta=eta.astimezone(UTC) if eta is not None else utcnow(),
                expires=expires,
                priority=priority,
                dedup_key=dedup_key,
            )
            .on_conflict_do_nothing(index_elements=["dedup_key"])
            .returning(Task)
        )
    ).one_or_none()
    if row is None:
        row = (await db.scalars(sa.select(Task).where(Task.dedup_key == dedup_key))).one()
        stored_expires = row.expires
        if stored_expires is not None and stored_expires.tzinfo is None:
            stored_expires = stored_expires.replace(tzinfo=UTC)
        if (row.name, row.queue, row.args, row.kwargs, stored_expires, row.priority) != (
            name,
            queue_name,
            arguments[0],
            arguments[1],
            expires,
            priority,
        ):
            raise ValueError("dedup_key already belongs to a different task or arguments.")
    return row
