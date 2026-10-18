"""Read APIs for the history timeline (admin views use these)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from obox_builder.apps import apps
from obox_builder.contrib.history.models import RowChange
from obox_builder.contrib.history.tracker import get_tracker
from obox_builder.core.utils.time import ensure_aware
from obox_builder.utils.strings import camel_to_snake

DEFAULT_PAGE_SIZE = 20
PAGE_SIZES = (10, 20, 50)


@dataclass(frozen=True, slots=True)
class FieldDiff:
    name: str
    label: str
    old: Any
    new: Any


@dataclass(frozen=True, slots=True)
class RowChangeEntry:
    id: int
    op: str
    occurred_at: datetime
    actor_label: str
    source: str
    object_repr: str
    diffs: tuple[FieldDiff, ...]
    redacted: tuple[str, ...]
    unknown_old: tuple[str, ...]
    truncated: bool


def model_label(model: type[Any]) -> str:
    return f"{model._model_label}.{camel_to_snake(model.__name__)}"


async def entry_count(db: AsyncSession, model: type[Any], pk: Any) -> int:
    label = model_label(model)
    stmt = select(func.count(RowChange.id)).where(
        RowChange.model_label == label,
        RowChange.object_pk == str(pk),
    )
    return int((await db.execute(stmt)).scalar_one())


async def history_entries(
    db: AsyncSession,
    model: type[Any],
    pk: Any,
    *,
    before: int | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
) -> list[RowChangeEntry]:
    """Newest-first entries for one row, keyset-paged with ``before`` (an id)."""
    label = model_label(model)
    stmt = select(RowChange).where(RowChange.model_label == label, RowChange.object_pk == str(pk))
    if before is not None:
        stmt = stmt.where(RowChange.id < before)
    stmt = stmt.order_by(RowChange.id.desc()).limit(limit)
    return [_to_entry(row) for row in (await db.scalars(stmt))]


def _to_entry(row: RowChange) -> RowChangeEntry:
    payload: dict[str, Any] = row.changes if isinstance(row.changes, dict) else {}
    field_changes: dict[str, Any] = payload.get("fields") or {}
    labels = _field_labels(row.model_label)
    diffs = tuple(
        FieldDiff(
            name=name,
            label=labels.get(name, name),
            old=value[0] if len(value) > 1 else None,
            new=value[1] if len(value) > 1 else None,
        )
        for name, value in sorted(field_changes.items())
    )
    redacted: list[str] = list(payload.get("redacted") or ())
    unknown_old: list[str] = list(payload.get("unknown_old") or ())
    return RowChangeEntry(
        id=row.id,
        op=row.op,
        occurred_at=ensure_aware(row.occurred_at),
        actor_label=row.actor_label,
        source=row.source,
        object_repr=row.object_repr,
        diffs=diffs,
        redacted=tuple(labels.get(name, name) for name in redacted),
        unknown_old=tuple(labels.get(name, name) for name in unknown_old),
        truncated=bool(payload.get("truncated")),
    )


def _field_labels(label: str) -> dict[str, str]:
    """``{field_name: label}`` from the tracker, with a readable fallback."""
    for model in apps.get_models():
        if model_label(model) == label:
            tracker = get_tracker(model)
            if tracker is None:
                break
            return {tracked.name: tracked.label for tracked in tracker.fields}
    return {}


def value_text(value: Any) -> str:
    """Render a payload value for display (truncated previews stay readable)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)
