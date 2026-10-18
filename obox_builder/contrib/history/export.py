"""Streaming export of history entries (NDJSON/CSV) for compliance hand-offs.

CSV cells that start with ``=``, ``+``, ``-`` or ``@`` are prefixed with ``'``
to defuse spreadsheet formula injection. Exports are read-only and every run
logs one line to the ``obox.history.audit`` logger.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from obox_builder.contrib.history.models import RowChange
from obox_builder.contrib.history.service import FeedFilters

logger = logging.getLogger("obox.history.audit")

EXPORT_BATCH_SIZE = 1_000
FORMULA_PREFIXES = ("=", "+", "-", "@")

CSV_COLUMNS = (
    "id",
    "occurred_at",
    "model_label",
    "object_pk",
    "object_repr",
    "op",
    "actor_id",
    "actor_label",
    "source",
    "request_id",
    "change_set_id",
    "changes",
)


def guard_cell(value: str) -> str:
    """Prefix spreadsheet formula starters so CSV opens safely."""
    return f"'{value}" if value.startswith(FORMULA_PREFIXES) else value


def as_dict(entry: RowChange) -> dict[str, Any]:
    return {
        "id": entry.id,
        "occurred_at": entry.occurred_at,
        "change_set_id": str(entry.change_set_id),
        "op": entry.op,
        "model_label": entry.model_label,
        "object_pk": entry.object_pk,
        "object_repr": entry.object_repr,
        "actor_id": entry.actor_id,
        "actor_label": entry.actor_label,
        "source": entry.source,
        "request_id": entry.request_id,
        "changes": entry.changes,
    }


def ndjson_line(entry: RowChange) -> str:
    return json.dumps(as_dict(entry), ensure_ascii=False, default=str) + "\n"


def csv_header() -> str:
    return ",".join(CSV_COLUMNS) + "\n"


def csv_line(entry: RowChange) -> str:
    values = as_dict(entry)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        [
            guard_cell(str(values[column]))
            if column != "changes"
            else json.dumps(values[column], ensure_ascii=False, default=str)
            for column in CSV_COLUMNS
        ]
    )
    return buffer.getvalue()


async def export_entries(
    db: AsyncSession,
    filters: FeedFilters | None = None,
    *,
    actor_id: int | None = None,
    batch_size: int = EXPORT_BATCH_SIZE,
) -> AsyncIterator[RowChange]:
    """Yield matching entries in id order, using keyset pagination."""
    conditions = filters.conditions() if filters is not None else []
    if actor_id is not None:
        conditions.append(RowChange.actor_id == actor_id)
    after_id = 0
    while True:
        stmt = (
            sa.select(RowChange)
            .where(*conditions, RowChange.id > after_id)
            .order_by(RowChange.id)
            .limit(batch_size)
        )
        rows = list(await db.scalars(stmt))
        if not rows:
            return
        for row in rows:
            yield row
        after_id = rows[-1].id


def parse_day(raw: str) -> datetime | None:
    """Parse ``YYYY-MM-DD`` into an aware midnight datetime."""
    try:
        parsed = date.fromisoformat(raw)
    except ValueError:
        return None
    return datetime(parsed.year, parsed.month, parsed.day, tzinfo=UTC)
