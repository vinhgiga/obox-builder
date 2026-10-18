"""Read APIs for the history timeline (admin views use these)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, false, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from obox_builder.apps import apps
from obox_builder.contrib.history.models import RowChange
from obox_builder.contrib.history.tracker import Tracker, get_tracker
from obox_builder.core.utils.time import ensure_aware
from obox_builder.utils.strings import camel_to_snake

DEFAULT_PAGE_SIZE = 20
PAGE_SIZES = (10, 20, 50)

#: The canonical form of a model label: ``app_label.model_name``.
MODEL_LABEL_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class FieldDiff:
    name: str
    label: str
    old: Any
    new: Any


@dataclass(frozen=True, slots=True)
class RelationDiff:
    name: str
    label: str
    added: tuple[str, ...]
    removed: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RowChangeEntry:
    id: int
    change_set_id: UUID
    op: str
    occurred_at: datetime
    actor_label: str
    source: str
    model_label: str
    object_pk: str
    object_repr: str
    diffs: tuple[FieldDiff, ...]
    m2m: tuple[RelationDiff, ...]
    #: Partially masked field changes (display-safe values; never revertable).
    masked: tuple[FieldDiff, ...]
    #: Recorded field *names* (labels are resolved at render time so views can
    #: filter by permission before anything reaches a template).
    redacted: tuple[str, ...]
    unknown_old: tuple[str, ...]
    truncated: bool
    truncated_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FeedFilters:
    """Global feed filters (F3): model/actor/source/op/time window."""

    #: ``None`` = every model; an empty set matches nothing.
    model_labels: frozenset[str] | None = None
    actor: str = ""
    source: str = ""
    op: str = ""
    since: datetime | None = None
    until: datetime | None = None

    def conditions(self) -> list[ColumnElement[bool]]:
        clauses: list[ColumnElement[bool]] = []
        if self.model_labels is not None:
            if not self.model_labels:
                return [false()]
            clauses.append(RowChange.model_label.in_(self.model_labels))
        if self.actor:
            clauses.append(RowChange.actor_label.ilike(f"%{self.actor}%"))
        if self.source:
            clauses.append(RowChange.source == self.source)
        if self.op:
            clauses.append(RowChange.op == self.op)
        if self.since is not None:
            clauses.append(RowChange.occurred_at >= self.since)
        if self.until is not None:
            clauses.append(RowChange.occurred_at < self.until)
        return clauses


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
    return [to_entry(row) for row in (await db.scalars(stmt))]


def to_entry(row: RowChange) -> RowChangeEntry:
    payload: dict[str, Any] = row.changes if isinstance(row.changes, dict) else {}
    field_changes: dict[str, Any] = payload.get("fields") or {}
    labels = field_labels(row.model_label)
    diffs = tuple(
        FieldDiff(
            name=name,
            label=labels.get(name, name),
            old=value[0] if len(value) > 1 else None,
            new=value[1] if len(value) > 1 else None,
        )
        for name, value in sorted(field_changes.items())
    )
    relation_labels = _relation_labels(row.model_label)
    relation_changes: dict[str, Any] = payload.get("m2m") or {}
    m2m = tuple(
        RelationDiff(
            name=name,
            label=relation_labels.get(name, name),
            added=tuple(sides.get("add") or ()),
            removed=tuple(sides.get("remove") or ()),
        )
        for name, sides in sorted(relation_changes.items())
        if isinstance(sides, dict)
    )
    masked_changes: dict[str, Any] = payload.get("masked") or {}
    masked = tuple(
        FieldDiff(
            name=name,
            label=labels.get(name, name),
            old=value[0] if isinstance(value, list) and len(value) > 1 else None,
            new=value[1] if isinstance(value, list) and len(value) > 1 else None,
        )
        for name, value in sorted(masked_changes.items())
    )
    redacted: list[str] = list(payload.get("redacted") or ())
    unknown_old: list[str] = list(payload.get("unknown_old") or ())
    truncated: dict[str, Any] = payload.get("truncated") or {}
    return RowChangeEntry(
        id=row.id,
        change_set_id=row.change_set_id,
        op=row.op,
        occurred_at=ensure_aware(row.occurred_at),
        actor_label=row.actor_label,
        source=row.source,
        model_label=row.model_label,
        object_pk=row.object_pk,
        object_repr=row.object_repr,
        diffs=diffs,
        m2m=m2m,
        masked=masked,
        redacted=tuple(redacted),
        unknown_old=tuple(unknown_old),
        truncated=bool(truncated),
        truncated_names=tuple(sorted(truncated)),
    )


async def feed_entries(
    db: AsyncSession,
    *,
    filters: FeedFilters | None = None,
    before: int | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
) -> list[RowChangeEntry]:
    """Newest-first global feed, keyset-paged with ``before`` (an id)."""
    stmt = select(RowChange)
    for condition in filters.conditions() if filters is not None else []:
        stmt = stmt.where(condition)
    if before is not None:
        stmt = stmt.where(RowChange.id < before)
    stmt = stmt.order_by(RowChange.id.desc()).limit(limit)
    return [to_entry(row) for row in (await db.scalars(stmt))]


async def feed_count(db: AsyncSession, *, filters: FeedFilters | None = None) -> int:
    stmt = select(func.count(RowChange.id))
    for condition in filters.conditions() if filters is not None else []:
        stmt = stmt.where(condition)
    return int((await db.execute(stmt)).scalar_one())


async def change_set_entries(
    db: AsyncSession,
    change_set_id: UUID,
    *,
    model_labels: frozenset[str] | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
) -> list[RowChangeEntry]:
    """Entries of one operation in chronological order."""
    stmt = select(RowChange).where(RowChange.change_set_id == change_set_id)
    if model_labels is not None:
        if not model_labels:
            return []
        stmt = stmt.where(RowChange.model_label.in_(model_labels))
    stmt = stmt.order_by(RowChange.id).limit(limit)
    return [to_entry(row) for row in (await db.scalars(stmt))]


def _tracker_for(label: str) -> Tracker | None:
    for model in apps.get_models():
        if model_label(model) == label:
            return get_tracker(model)
    return None


def model_for_label(label: str) -> type[Any] | None:
    """The model a ``model_label`` refers to, or ``None`` when unregistered."""
    tracker = _tracker_for(label)
    return tracker.model if tracker is not None else None


def field_labels(label: str) -> dict[str, str]:
    """``{field_name: label}`` from the tracker, with a readable fallback."""
    tracker = _tracker_for(label)
    if tracker is None:
        return {}
    return {tracked.name: tracked.label for tracked in tracker.fields}


def _relation_labels(label: str) -> dict[str, str]:
    tracker = _tracker_for(label)
    if tracker is None:
        return {}
    return {relation.name: relation.label for relation in tracker.relations}


def value_text(value: Any) -> str:
    """Render a payload value for display (truncated previews stay readable)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)
