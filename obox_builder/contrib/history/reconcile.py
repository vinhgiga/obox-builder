"""``history_reconcile``: detect (and optionally repair) capture blind spots.

Raw SQL, other services and Core bulk DML bypass the ORM capture path, so a
row can drift away from its recorded history. Reconcile replays each row's
entries (create snapshots + subsequent diffs) to reconstruct the last recorded
values, compares them with the current table row and reports:

* ``missing`` — history exists but the row is gone while the last op is not
  ``delete``;
* ``resurrected`` — the last op is ``delete`` but the row is back;
* ``unexpected`` — the row exists without any history (blind insert);
* ``stale`` — recorded values differ from the current row.

With ``--fix`` a repair entry (``source="db_trigger"``) is appended for each
drift, so the timeline matches reality afterwards. Masked/redacted/truncated
values are never compared or repaired (their plain value is unknown), and the
repair entry records the fields it can see.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from obox_builder.contrib.history.models import RowChange
from obox_builder.contrib.history.payload import SCHEMA_VERSION
from obox_builder.contrib.history.service import model_for_label
from obox_builder.contrib.history.tracker import Tracker, get_tracker
from obox_builder.core.utils.time import utcnow

logger = logging.getLogger("obox.history.audit")

MAX_BATCH_SIZE = 10_000
REPAIR_LABEL = "history_reconcile"


@dataclass
class Drift:
    """One drifted row and why."""

    model_label: str
    object_pk: str
    kind: str  # missing | resurrected | unexpected | stale
    detail: str = ""

    def describe(self) -> str:
        suffix = f" ({self.detail})" if self.detail else ""
        return f"{self.model_label}#{self.object_pk}: {self.kind}{suffix}"


@dataclass
class ReconcileResult:
    """What one ``history_reconcile`` run found (and fixed)."""

    checked: int = 0
    drifts: list[Drift] = field(default_factory=list)
    skipped_labels: list[str] = field(default_factory=list)
    fixed: int = 0
    dry_run: bool = True


@dataclass
class _Replay:
    """Recorded state of one row while replaying its timeline (oldest first)."""

    values: dict[str, Any] = field(default_factory=dict)
    unknown: set[str] = field(default_factory=set)
    last_op: str = ""

    def apply(self, row: RowChange) -> None:
        payload = row.changes if isinstance(row.changes, dict) else {}
        self.last_op = row.op
        if row.op == "create":
            # Both ORM and reconcile-created entries snapshot every field.
            self.values = {}
            self.unknown = set()
        recorded = payload.get("fields") or {}
        for name, value in recorded.items():
            if isinstance(value, list) and value:
                self.values[name] = value[-1]
        self.unknown.update(payload.get("redacted") or ())
        self.unknown.update(payload.get("masked") or {})
        self.unknown.update(payload.get("truncated") or {})
        self.unknown.update(payload.get("unknown_old") or ())


def _tracker_for_label(label: str) -> Tracker | None:
    model = model_for_label(label)
    return None if model is None else get_tracker(model)


def _pk_value(raw: str) -> Any:
    try:
        return int(raw)
    except ValueError:
        return raw


def _pk_column(tracker: Tracker) -> Any:
    return getattr(tracker.model, tracker.pk_key)


def _replay(rows: list[RowChange]) -> dict[str, _Replay]:
    replayed: dict[str, _Replay] = {}
    for row in rows:
        replayed.setdefault(row.object_pk, _Replay()).apply(row)
    return replayed


def _stale_fields(tracker: Tracker, obj: Any, replay: _Replay) -> list[str]:
    stale: list[str] = []
    for tracked in tracker.fields:
        if tracked.name in replay.unknown or tracked.name not in replay.values:
            continue
        current = tracked.spec.type.diff_repr(getattr(obj, tracked.name))
        if current != replay.values[tracked.name]:
            stale.append(tracked.name)
    return sorted(stale)


def _payload(tracker: Tracker, obj: Any, replay: _Replay | None) -> dict[str, Any]:
    """A full-snapshot payload for a repair entry (``new`` = current values).

    Secret/masked fields and values whose plain form is unknown are skipped:
    a repair must never write data the normal capture path would redact.
    """
    pairs: dict[str, list[Any]] = {}
    for tracked in tracker.fields:
        if tracked.secret or tracked.masked:
            continue
        if replay is not None and tracked.name in replay.unknown:
            continue
        if obj is None:
            if replay is not None and tracked.name in replay.values:
                pairs[tracked.name] = [replay.values[tracked.name], None]
            continue
        current = tracked.spec.type.diff_repr(getattr(obj, tracked.name))
        old = None if replay is None else replay.values.get(tracked.name)
        pairs[tracked.name] = [old, current]
    return {
        "fields": pairs,
        "m2m": {},
        "redacted": [],
        "masked": {},
        "truncated": {},
        "unknown_old": [],
    }


def _repair(
    tracker: Tracker, *, op: str, object_pk: str, obj: Any | None, replay: _Replay | None
) -> RowChange:
    return RowChange(
        occurred_at=utcnow(),
        change_set_id=uuid.uuid4(),
        op=op,
        model_label=tracker.model_label,
        object_pk=object_pk[:255],
        object_repr=(tracker.object_repr(obj) if obj is not None else "")[:255],
        changes=_payload(tracker, obj, replay),
        schema_version=SCHEMA_VERSION,
        row_version=None,
        actor_id=None,
        actor_label=REPAIR_LABEL,
        source="db_trigger",
    )


async def _labels(db: AsyncSession, model: str | None) -> list[str]:
    if model:
        return [model]
    stmt = sa.select(RowChange.model_label).distinct().order_by(RowChange.model_label)
    return list(await db.scalars(stmt))


async def _reconcile_label(
    db: AsyncSession,
    tracker: Tracker,
    *,
    batch_size: int,
    fix: bool,
    result: ReconcileResult,
) -> None:
    after_pk = ""
    while True:
        pks = list(
            await db.scalars(
                sa.select(RowChange.object_pk)
                .where(
                    RowChange.model_label == tracker.model_label,
                    RowChange.object_pk > after_pk,
                )
                .distinct()
                .order_by(RowChange.object_pk)
                .limit(batch_size)
            )
        )
        if not pks:
            break
        after_pk = pks[-1]
        history = list(
            await db.scalars(
                sa.select(RowChange)
                .where(
                    RowChange.model_label == tracker.model_label,
                    RowChange.object_pk.in_(pks),
                )
                .order_by(RowChange.object_pk, RowChange.id)
            )
        )
        replayed = _replay(history)
        ids = [_pk_value(pk) for pk in pks]
        rows = list(await db.scalars(sa.select(tracker.model).where(_pk_column(tracker).in_(ids))))
        current = {str(tracker.pk(obj)): obj for obj in rows}
        for pk in pks:
            replay = replayed[pk]
            result.checked += 1
            obj = current.get(pk)
            if obj is None:
                if replay.last_op == "delete":
                    continue
                result.drifts.append(
                    Drift(tracker.model_label, pk, "missing", f"last op {replay.last_op}")
                )
                if fix:
                    db.add(_repair(tracker, op="delete", object_pk=pk, obj=None, replay=replay))
                    result.fixed += 1
                continue
            if replay.last_op == "delete":
                result.drifts.append(Drift(tracker.model_label, pk, "resurrected"))
                if fix:
                    db.add(_repair(tracker, op="create", object_pk=pk, obj=obj, replay=replay))
                    result.fixed += 1
                continue
            stale = _stale_fields(tracker, obj, replay)
            if stale:
                result.drifts.append(
                    Drift(tracker.model_label, pk, "stale", f"fields: {', '.join(stale)}")
                )
                if fix:
                    db.add(_repair(tracker, op="update", object_pk=pk, obj=obj, replay=replay))
                    result.fixed += 1
        if fix:
            await db.commit()


async def _scan_unexpected(
    db: AsyncSession, tracker: Tracker, *, fix: bool, result: ReconcileResult
) -> None:
    """Rows in the table that never appeared in history (blind inserts)."""
    known = set(
        await db.scalars(
            sa.select(RowChange.object_pk).where(RowChange.model_label == tracker.model_label)
        )
    )
    for obj in await db.scalars(sa.select(tracker.model)):
        pk = str(tracker.pk(obj))
        if pk in known:
            continue
        known.add(pk)
        result.drifts.append(Drift(tracker.model_label, pk, "unexpected", "no history entries"))
        if fix:
            db.add(_repair(tracker, op="create", object_pk=pk, obj=obj, replay=None))
            result.fixed += 1


async def reconcile(
    db: AsyncSession,
    *,
    model: str | None = None,
    batch_size: int = MAX_BATCH_SIZE,
    fix: bool = False,
) -> ReconcileResult:
    """Compare tracked rows against their replayed history, label by label."""
    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}.")
    result = ReconcileResult(dry_run=not fix)
    for label in await _targets(db, model, result):
        tracker = _tracker_for_label(label)
        if tracker is None:
            result.skipped_labels.append(label)
            continue
        await _reconcile_label(db, tracker, batch_size=batch_size, fix=fix, result=result)
        await _scan_unexpected(db, tracker, fix=fix, result=result)
        if fix:
            await db.commit()
    if fix:
        await db.commit()
    logger.info(
        "history_reconcile: checked %d row(s), %d drift(s), %d repaired%s.",
        result.checked,
        len(result.drifts),
        result.fixed,
        "" if fix else " (dry-run)",
    )
    return result


async def _targets(db: AsyncSession, model: str | None, result: ReconcileResult) -> list[str]:
    """Labels to scan: the requested one, else every tracked model plus
    labels that only exist in history (renamed/unregistered models)."""
    if model:
        return [model]
    from obox_builder.apps import apps
    from obox_builder.contrib.history.service import model_label
    from obox_builder.contrib.history.tracker import is_tracked

    labels = {model_label(candidate) for candidate in apps.get_models() if is_tracked(candidate)}
    labels.update(await _labels(db, None))
    return sorted(labels)
