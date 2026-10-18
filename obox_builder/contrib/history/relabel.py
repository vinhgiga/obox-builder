"""``history_relabel``: re-point history entries at a renamed model label.

Renaming ``app.Model`` orphans old entries under the previous ``model_label``
(the timeline, feed and permissions resolve labels against the current app
registry). Relabeling rewrites the label in place — it is a maintenance
operation on the *metadata* of immutable entries, so it runs under the same
guard-suspension as ``history_anonymize`` and is logged to
``obox.history.audit``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from obox_builder.contrib.history.immutability import (
    begin_maintenance,
    end_maintenance,
    per_transaction_maintenance,
)
from obox_builder.contrib.history.models import RowChange

logger = logging.getLogger("obox.history.audit")

MAX_BATCH_SIZE = 10_000


@dataclass
class RelabelResult:
    """What one relabel run did (or would do, with ``dry_run``)."""

    updated: int = 0
    batches: int = 0
    dry_run: bool = False


async def relabel(
    db: AsyncSession,
    *,
    from_label: str,
    to_label: str,
    batch_size: int = MAX_BATCH_SIZE,
    dry_run: bool = False,
) -> RelabelResult:
    """Rewrite ``model_label`` from ``from_label`` to ``to_label`` in batches."""
    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}.")
    if from_label == to_label:
        raise ValueError("from_label and to_label must differ.")
    result = RelabelResult(dry_run=dry_run)
    if dry_run:
        result.updated = int(
            await db.scalar(
                sa.select(sa.func.count(RowChange.id)).where(RowChange.model_label == from_label)
            )
            or 0
        )
        return result
    mode = await begin_maintenance(db)
    try:
        while True:
            await per_transaction_maintenance(db, mode)
            expired = (
                sa.select(RowChange.id)
                .where(RowChange.model_label == from_label)
                .order_by(RowChange.id)
                .limit(batch_size)
            )
            outcome = await db.execute(
                sa.update(RowChange).where(RowChange.id.in_(expired)).values(model_label=to_label)
            )
            await db.commit()
            updated = int(getattr(outcome, "rowcount", 0) or 0)
            result.updated += updated
            if not updated:
                break
            result.batches += 1
            logger.info(
                "history_relabel: rewrote %d row(s) %s -> %s.", updated, from_label, to_label
            )
            if updated < batch_size:
                break
    finally:
        await end_maintenance(db, mode)
    logger.info(
        "history_relabel: %s -> %s: %d row(s) in %d batch(es).",
        from_label,
        to_label,
        result.updated,
        result.batches,
    )
    return result
