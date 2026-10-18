"""SQLAlchemy session-event capture.

Tracked objects are turned into pending diffs in ``before_flush`` and written
in ``after_flush`` (inside the transaction that mutates the data, so commit is
atomic; the insert uses Core-only, so it cannot recurse into the flush).
Designed for ``expire_on_commit=False`` sessions (see ``db.session``): on
``after_commit`` the pending queue is simply dropped.

Failure policy (``HISTORY_FAILURE_MODE``):

* ``closed`` (default): a history failure rolls the transaction back.
* ``open``: the failure is logged and swallowed. On backends with savepoints
  (PostgreSQL) a ``SAVEPOINT`` is used so the data change can still commit.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import event, insert
from sqlalchemy.orm import Session as OrmSession

from obox_builder.apps import apps
from obox_builder.conf import settings
from obox_builder.contrib.history import signals
from obox_builder.contrib.history.context import HistoryContext, resolve_context
from obox_builder.contrib.history.models import RowChange
from obox_builder.contrib.history.payload import SCHEMA_VERSION, build_changes
from obox_builder.contrib.history.tracker import PendingChange, get_tracker
from obox_builder.core.utils.time import utcnow

logger = logging.getLogger("obox.history")

PENDING_KEY = "obox.history.pending"
CHANGE_SET_KEY = "obox.history.change_set"

_listeners_ready = False


class BulkWriteNotTrackedError(RuntimeError):
    """Raised (``HISTORY_BULK_POLICY='raise'``) for bulk DML on tracked models."""


def ensure_listeners() -> None:
    """Attach process-wide listeners once (called from ``HistoryConfig.ready``)."""
    global _listeners_ready
    if _listeners_ready:
        return
    _listeners_ready = True
    event.listen(OrmSession, "before_flush", _before_flush)
    event.listen(OrmSession, "after_flush", _after_flush)
    event.listen(OrmSession, "after_commit", _after_commit)
    event.listen(OrmSession, "after_rollback", _after_rollback)
    event.listen(OrmSession, "do_orm_execute", _on_orm_execute)


def _enabled() -> bool:
    return bool(
        settings.HISTORY_ENABLED
        and apps.models_ready
        and apps.is_installed("obox_builder.contrib.history")
    )


def _before_flush(session: OrmSession, flush_context: Any, instances: Any) -> None:
    if not _enabled():
        return
    pending: list[PendingChange] = []
    seen: set[int] = set()
    for obj, op in (
        *((obj, "create") for obj in session.new),
        *((obj, "update") for obj in session.dirty),
        *((obj, "delete") for obj in session.deleted),
    ):
        if id(obj) in seen:
            continue
        seen.add(id(obj))
        tracker = get_tracker(type(obj))
        if tracker is None:
            continue
        change = tracker.diff(obj, op)
        if change is not None:
            pending.append(change)
    session.info[PENDING_KEY] = pending
    if pending:
        _change_set_id(session)
        # The live list is passed on purpose: receivers may mutate or prune it.
        signals.pre_record.send(sender=RowChange, changes=pending)


def _after_flush(session: OrmSession, flush_context: Any) -> None:
    pending: list[PendingChange] = session.info.pop(PENDING_KEY, None) or []
    if not pending:
        return
    for change in pending:
        if change.op == "create":
            # Row snapshots need the column defaults applied by the INSERT.
            change.tracker.fill_create(change)
        # M2M ids need the PKs assigned by the INSERT (self or related rows).
        change.tracker.fill_m2m(change)
    pending = [change for change in pending if change.has_effect]
    if not pending:
        return
    try:
        rows = _insert_rows(session, pending)
    except Exception:
        if settings.HISTORY_FAILURE_MODE == "closed":
            raise
        logger.exception("Failed to record row history (HISTORY_FAILURE_MODE='open').")
        return
    signals.post_record.send(sender=RowChange, rows=rows)


def _after_commit(session: OrmSession) -> None:
    # The session was flushed and committed: the payload belonging to this
    # flush is no longer useful, so make sure it cannot leak into the next one.
    session.info.pop(PENDING_KEY, None)
    session.info.pop(CHANGE_SET_KEY, None)


def _after_rollback(session: OrmSession) -> None:
    session.info.pop(PENDING_KEY, None)
    session.info.pop(CHANGE_SET_KEY, None)


def _change_set_id(session: OrmSession) -> uuid.UUID:
    change_set = session.info.get(CHANGE_SET_KEY)
    if change_set is None:
        change_set = uuid.uuid4()
        session.info[CHANGE_SET_KEY] = change_set
    return change_set


def _insert_rows(session: OrmSession, pending: list[PendingChange]) -> list[dict[str, Any]]:
    context = resolve_context(session)
    change_set_id = _change_set_id(session)
    occurred_at = utcnow()
    rows = [_row_for(change, context, change_set_id, occurred_at) for change in pending]
    if settings.HISTORY_FAILURE_MODE == "open":
        # A failed statement has already poisoned the transaction; a savepoint
        # lets the data change commit on backends that support it.
        with session.connection().begin_nested():
            session.execute(insert(RowChange), rows)
        return rows
    session.execute(insert(RowChange), rows)
    return rows


def _row_for(
    change: PendingChange,
    context: HistoryContext,
    change_set_id: uuid.UUID,
    occurred_at: datetime,
) -> dict[str, Any]:
    tracker = change.tracker
    obj = change.obj
    return {
        "occurred_at": occurred_at,
        "change_set_id": change_set_id,
        "op": change.op,
        "model_label": tracker.model_label,
        "object_pk": str(tracker.pk(obj))[:255],
        "object_repr": tracker.object_repr(obj),
        "changes": build_changes(change),
        "schema_version": SCHEMA_VERSION,
        "row_version": None,
        "actor_id": context.actor_id,
        "actor_label": (context.actor_label or "system")[:150],
        "source": context.source,
        "request_id": context.request_id,
        "ip": context.ip,
        "user_agent": context.user_agent,
        "reason": context.reason,
        "reverts_id": context.reverts_id,
    }


def _on_orm_execute(execute_state: Any) -> None:
    """Warn/raise for bulk ORM DML that bypasses the unit of work."""
    if not _enabled():
        return
    policy = settings.HISTORY_BULK_POLICY
    if policy == "ignore":
        return
    if not (execute_state.is_update or execute_state.is_delete):
        return
    models = {mapper.class_ for mapper in execute_state.all_mappers}
    tracked = sorted(
        (model for model in models if get_tracker(model) is not None),
        key=lambda model: model.__name__,
    )
    if not tracked:
        return
    labels = ", ".join(f"{model._model_label}.{model.__name__}" for model in tracked)
    message = (
        f"Bulk {'update' if execute_state.is_update else 'delete'} bypasses row "
        f"history for: {labels}. Use per-row ORM deletes/updates (e.g. "
        "admin bulk actions) or HISTORY_BULK_POLICY='ignore' to silence this."
    )
    if policy == "raise":
        raise BulkWriteNotTrackedError(message)
    logger.warning(message)
