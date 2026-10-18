"""Retention: delete expired history entries in bounded, logged batches.

``history_purge`` uses keyset-ordered batches (≤10k rows) with a pause between
them, so a large purge never holds long locks. PostgreSQL's append-only trigger
is bypassed per batch with ``SET LOCAL obox.history_maintenance = 'on'`` (SQLite
allows ``DELETE``). Retention comes from ``--days``, else the per-model
``__history__["retention_days"]``, else ``HISTORY_RETENTION_DAYS``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from obox_builder.conf import settings
from obox_builder.contrib.history.immutability import MAINTENANCE_GUC
from obox_builder.contrib.history.models import RowChange
from obox_builder.contrib.history.service import model_label
from obox_builder.core.utils.time import utcnow

logger = logging.getLogger("obox.history")

MAX_BATCH_SIZE = 10_000
BATCH_PAUSE_SECONDS = 0.05


@dataclass
class PurgeResult:
    """What one ``history_purge`` run did (or would do, with ``dry_run``)."""

    deleted: int = 0
    batches: int = 0
    per_label: dict[str, int] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    dry_run: bool = False


def retention_overrides() -> dict[str, int | None]:
    """Per-model ``retention_days`` keyed by model label (explicit wins)."""
    from obox_builder.contrib.history.tracker import declared_models

    return {model_label(model): options.retention_days for model, options in declared_models()}


def effective_days(
    label: str, explicit: int | None, overrides: dict[str, int | None]
) -> int | None:
    if explicit is not None:
        return explicit
    return overrides.get(label, settings.HISTORY_RETENTION_DAYS)


async def _labels(db: AsyncSession, model: str | None) -> list[str]:
    if model:
        return [model]
    stmt = sa.select(RowChange.model_label).distinct().order_by(RowChange.model_label)
    return list(await db.scalars(stmt))


async def _delete_batches(
    db: AsyncSession,
    label: str,
    cutoff: datetime,
    batch_size: int,
    pause: float,
    result: PurgeResult,
) -> None:
    is_postgres = db.get_bind().dialect.name == "postgresql"
    while True:
        if is_postgres:
            await db.execute(sa.text(MAINTENANCE_GUC))
        expired = (
            sa.select(RowChange.id)
            .where(RowChange.model_label == label, RowChange.occurred_at < cutoff)
            .order_by(RowChange.id)
            .limit(batch_size)
        )
        outcome = await db.execute(sa.delete(RowChange).where(RowChange.id.in_(expired)))
        await db.commit()
        deleted = int(getattr(outcome, "rowcount", 0) or 0)
        result.deleted += deleted
        result.per_label[label] = result.per_label.get(label, 0) + deleted
        if deleted:
            result.batches += 1
            logger.info(
                "history_purge: deleted %d row(s) for %s before %s.",
                deleted,
                label,
                cutoff.isoformat(),
            )
        if deleted < batch_size:
            return
        if pause:
            await asyncio.sleep(pause)


async def purge(
    db: AsyncSession,
    *,
    days: int | None = None,
    model: str | None = None,
    batch_size: int = MAX_BATCH_SIZE,
    pause: float = BATCH_PAUSE_SECONDS,
    dry_run: bool = False,
) -> PurgeResult:
    """Delete entries older than the effective retention, label by label.

    Commits each batch (PG needs a fresh transaction per ``SET LOCAL``), so the
    caller should not wrap this in its own transaction.
    """
    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}.")
    if days is not None and days < 0:
        raise ValueError("days must be >= 0.")
    result = PurgeResult(dry_run=dry_run)
    overrides = retention_overrides()
    for label in await _labels(db, model):
        retention = effective_days(label, days, overrides)
        if retention is None:
            result.skipped.append(label)
            continue
        cutoff = utcnow() - timedelta(days=retention)
        if dry_run:
            count = await db.scalar(
                sa.select(sa.func.count(RowChange.id)).where(
                    RowChange.model_label == label, RowChange.occurred_at < cutoff
                )
            )
            result.deleted += int(count or 0)
            result.per_label[label] = int(count or 0)
            continue
        await _delete_batches(db, label, cutoff, batch_size, pause, result)
    if result.skipped:
        logger.info(
            "history_purge: no retention for %s (set --days or HISTORY_RETENTION_DAYS).",
            ", ".join(result.skipped),
        )
    logger.info(
        "history_purge: %s %d row(s) in %d batch(es).",
        "would delete" if dry_run else "deleted",
        result.deleted,
        result.batches,
    )
    return result
