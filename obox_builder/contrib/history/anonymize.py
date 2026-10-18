"""GDPR erasure for row history: replace identifying data with opaque tokens.

Two scopes (never deleting entries, so the timeline structure stays intact):

* ``anonymize_actor`` — for one actor id: blank ``actor_id``/``actor_label``/
  ``ip``/``user_agent`` on every entry of that actor.
* ``anonymize_object`` — for one row: erase the recorded values (fields, masked
  values, M2M lists, truncation info) and the ``object_repr``, keeping the
  entry skeleton (who/when/which fields changed).

Maintenance runs under ``obox.history_maintenance`` on PostgreSQL; on SQLite the
append-only UPDATE trigger is dropped for the run and recreated afterwards.
Every batch and the run summary are logged to ``obox.history.audit``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from obox_builder.contrib.history.immutability import (
    MAINTENANCE_GUC,
    SQLITE_CREATE_TRIGGER,
    SQLITE_DROP_TRIGGER,
)
from obox_builder.contrib.history.models import RowChange

logger = logging.getLogger("obox.history.audit")

MAX_BATCH_SIZE = 10_000
ANONYMOUS_LABEL = "anonymous"


@dataclass
class AnonymizeResult:
    """What one anonymize run did (or would do, with ``dry_run``)."""

    updated: int = 0
    batches: int = 0
    dry_run: bool = False


def erase_payload(payload: Any) -> dict[str, Any]:
    """Replace recorded values with redacted markers, keeping the skeleton."""
    data: dict[str, Any] = payload if isinstance(payload, dict) else {}
    fields = data.get("fields") or {}
    masked = data.get("masked") or {}
    names = sorted({*fields, *masked})
    redacted = sorted({*(data.get("redacted") or ()), *names})
    return {
        "fields": {},
        "m2m": {},
        "redacted": redacted,
        "masked": {},
        "truncated": {},
        "unknown_old": sorted(data.get("unknown_old") or ()),
    }


async def _start_maintenance(db: AsyncSession) -> str | None:
    """Suspend the append-only trigger; return the mode for ``_end_maintenance``."""
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        return "postgresql"
    if dialect == "sqlite":
        await db.execute(sa.text(SQLITE_DROP_TRIGGER))
        return "sqlite"
    return None


async def _end_maintenance(db: AsyncSession, mode: str | None) -> None:
    if mode == "sqlite":
        await db.execute(sa.text(SQLITE_CREATE_TRIGGER))
        await db.commit()


async def _set_maintenance(db: AsyncSession, mode: str | None) -> None:
    """Per-transaction maintenance switch on PostgreSQL (``SET LOCAL``)."""
    if mode == "postgresql":
        await db.execute(sa.text(MAINTENANCE_GUC))


async def anonymize_actor(
    db: AsyncSession,
    actor_id: int,
    *,
    batch_size: int = MAX_BATCH_SIZE,
    dry_run: bool = False,
) -> AnonymizeResult:
    """Blank the actor trail of one user without deleting entries."""
    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}.")
    result = AnonymizeResult(dry_run=dry_run)
    if dry_run:
        result.updated = await _actor_count(db, actor_id)
        return result
    mode = await _start_maintenance(db)
    try:
        while True:
            await _set_maintenance(db, mode)
            expired = (
                sa.select(RowChange.id)
                .where(RowChange.actor_id == actor_id)
                .order_by(RowChange.id)
                .limit(batch_size)
            )
            outcome = await db.execute(
                sa.update(RowChange)
                .where(RowChange.id.in_(expired))
                .values(
                    actor_id=None,
                    actor_label=ANONYMOUS_LABEL,
                    ip=None,
                    user_agent=None,
                )
            )
            await db.commit()
            updated = int(getattr(outcome, "rowcount", 0) or 0)
            result.updated += updated
            if not updated:
                break
            result.batches += 1
            logger.info("history_anonymize: blanked actor on %d row(s).", updated)
            if updated < batch_size:
                break
    finally:
        await _end_maintenance(db, mode)
    logger.info(
        "history_anonymize: actor_id=%s -> %d row(s) in %d batch(es).",
        actor_id,
        result.updated,
        result.batches,
    )
    return result


async def anonymize_object(
    db: AsyncSession,
    *,
    model_label: str,
    object_pk: str,
    batch_size: int = MAX_BATCH_SIZE,
    dry_run: bool = False,
) -> AnonymizeResult:
    """Erase the recorded values of one row (all entries), keeping skeletons."""
    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}.")
    result = AnonymizeResult(dry_run=dry_run)
    condition = (RowChange.model_label == model_label, RowChange.object_pk == object_pk)
    if dry_run:
        result.updated = int(
            await db.scalar(sa.select(sa.func.count(RowChange.id)).where(*condition)) or 0
        )
        return result
    mode = await _start_maintenance(db)
    after_id = 0
    try:
        while True:
            await _set_maintenance(db, mode)
            rows = (
                await db.execute(
                    sa.select(RowChange.id, RowChange.changes, RowChange.object_repr)
                    .where(*condition, RowChange.id > after_id)
                    .order_by(RowChange.id)
                    .limit(batch_size)
                )
            ).all()
            if not rows:
                break
            await db.execute(
                sa.update(RowChange),
                [
                    {
                        "id": row.id,
                        "changes": erase_payload(row.changes),
                        "object_repr": f"#{object_pk}"[:255],
                    }
                    for row in rows
                ],
            )
            await db.commit()
            result.updated += len(rows)
            result.batches += 1
            after_id = rows[-1].id
            logger.info(
                "history_anonymize: erased values on %d row(s) of %s#%s.",
                len(rows),
                model_label,
                object_pk,
            )
            if len(rows) < batch_size:
                break
    finally:
        await _end_maintenance(db, mode)
    logger.info(
        "history_anonymize: %s#%s -> %d row(s) in %d batch(es).",
        model_label,
        object_pk,
        result.updated,
        result.batches,
    )
    return result


async def _actor_count(db: AsyncSession, actor_id: int) -> int:
    count = await db.scalar(
        sa.select(sa.func.count(RowChange.id)).where(RowChange.actor_id == actor_id)
    )
    return int(count or 0)
