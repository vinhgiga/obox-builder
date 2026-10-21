"""Outbox capture inside the producer transaction, including SAVEPOINTs."""

from __future__ import annotations

import logging
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session
from sqlalchemy.orm.base import NO_VALUE

from obox_builder.conf import settings
from obox_builder.contrib.events.models import Delivery, Event
from obox_builder.contrib.events.options import get_tracker
from obox_builder.contrib.events.wake import notify
from obox_builder.contrib.history.context import resolve_context
from obox_builder.contrib.history.payload import build_changes
from obox_builder.contrib.history.tracker import PendingChange
from obox_builder.core.events import (
    DRAFTS_KEY,
    EventDraft,
    enabled,
    encode_payload,
    lineage,
    subscriptions,
)
from obox_builder.core.utils.time import utcnow

logger = logging.getLogger("obox.events")
PENDING = "obox.events.pending"
QUEUES = "obox.events.queues"
_installed = False


def ensure_listeners() -> None:
    global _installed
    if _installed:
        return
    for name, listener in (
        ("before_flush", _before_flush),
        ("after_flush", _after_flush),
        ("before_commit", _before_commit),
        ("after_commit", _after_commit),
        ("after_transaction_end", _transaction_end),
        ("do_orm_execute", _bulk_guard),
    ):
        event.listen(Session, name, listener)
    _installed = True


def _before_flush(session: Session, flush_context: Any, instances: Any) -> None:
    if any(isinstance(obj, Event) for obj in session.deleted) or any(
        isinstance(obj, Event) and session.is_modified(obj) for obj in session.dirty
    ):
        raise ValueError("Event envelopes are append-only.")
    if not enabled():
        return
    pending: list[PendingChange] = []
    seen: set[int] = set()
    for objects, op in (
        (session.new, "create"),
        (session.dirty, "update"),
        (session.deleted, "delete"),
    ):
        for obj in objects:
            if id(obj) in seen:
                continue
            seen.add(id(obj))
            tracked = get_tracker(type(obj))
            if tracked is not None:
                change = tracked[0].diff(obj, op)
                if change is None and op == "delete":
                    change = PendingChange(tracker=tracked[0], op=op, obj=obj)
                if change is not None:
                    pending.append(change)
    session.info[PENDING] = pending


def _after_flush(session: Session, flush_context: Any) -> None:
    pending = session.info.pop(PENDING, [])
    if not enabled():
        return
    rows: list[dict[str, Any]] = []
    for change in pending:
        if change.op == "create":
            change.tracker.fill_create(change)
        change.tracker.fill_m2m(change)
        if change.op == "update" and not change.has_effect:
            continue
        rows.append(_change_row(session, change))
    rows.extend(_draft_rows(session))
    _write(session, rows)


def _before_commit(session: Session) -> None:
    if session.info.get("obox.events.handler") and not session.in_nested_transaction():
        raise RuntimeError("Transactional event handlers must not commit their session.")
    if enabled():
        # Clean sessions do not invoke flush hooks. Flush first so draft
        # subjects have generated PKs, then drain any drafts still waiting.
        session.flush()
        _write(session, _draft_rows(session))


def _draft_rows(session: Session) -> list[dict[str, Any]]:
    current = session.get_nested_transaction() or session.get_transaction()
    drafts: list[EventDraft] = session.info.get(DRAFTS_KEY, [])
    selected = [draft for draft in drafts if draft.transaction is current]
    session.info[DRAFTS_KEY] = [draft for draft in drafts if draft.transaction is not current]
    rows = []
    for draft in selected:
        row = _envelope(session, draft.id, draft.type, draft.payload, draft.parent)
        row.update(
            subject=_subject(draft.subject),
            dedup_key=draft.dedup_key,
            schema_version=draft.schema_version,
        )
        rows.append(row)
    return rows


def _subject(subject: Any) -> str | None:
    if subject is None:
        return None
    if isinstance(subject, str):
        text = subject
    else:
        state = sa.inspect(subject)
        mapper = state.mapper
        if len(mapper.primary_key) != 1:
            raise ValueError("Event subjects require a single-column primary key.")
        pk = state.attrs[mapper.primary_key[0].key].loaded_value
        if pk is None or pk is NO_VALUE:
            raise ValueError("Event subject must be persisted in this transaction.")
        from obox_builder.utils.strings import camel_to_snake

        text = f"{subject._model_label}.{camel_to_snake(type(subject).__name__)}:{pk}"
    if len(text) > 512:
        raise ValueError("Event subject exceeds 512 characters.")
    return text


def _envelope(
    session: Session,
    id: uuid.UUID,
    type: str,
    payload: dict[str, Any],
    parent: Any,
) -> dict[str, Any]:
    ctx = resolve_context(session)
    hop = parent.hop + 1 if parent is not None else 0
    if hop > settings.EVENTS_MAX_HOPS:
        raise ValueError("Event chain exceeds EVENTS_MAX_HOPS.")
    return {
        "id": id,
        "type": type,
        "payload": encode_payload(payload),
        "occurred_at": utcnow(),
        "schema_version": 1,
        "subject": None,
        "dedup_key": None,
        "changed_fields": [],
        "source": ctx.source[:32],
        "actor_id": ctx.actor_id,
        "actor_label": ctx.actor_label[:150],
        "request_id": ctx.request_id,
        "causation_id": parent.event_id if parent is not None else None,
        "producer": parent.subscription if parent is not None else None,
        "hop": hop,
    }


def _change_row(session: Session, change: PendingChange) -> dict[str, Any]:
    diff = build_changes(change)
    values = {**diff["fields"], **diff["masked"]}
    options = get_tracker(type(change.obj))
    payload: dict[str, Any] = {
        "op": change.op,
        "model": change.tracker.model_label,
        "pk": str(change.tracker.pk(change.obj)),
        "row_version": change.tracker.row_version(change.obj),
        "fields": {key: pair[1] for key, pair in values.items()},
        "m2m": diff["m2m"],
        "redacted": diff["redacted"],
        "unknown_old": diff["unknown_old"],
        "truncated": {
            name: {side: meta for side, meta in sides.items() if side == "new"}
            for name, sides in diff["truncated"].items()
            if "new" in sides
        },
    }
    if options is not None and options[1].previous:
        payload["previous"] = {key: pair[0] for key, pair in values.items()}
        payload["truncated"] = diff["truncated"]
    row = _envelope(
        session, uuid.uuid4(), f"{change.tracker.model_label}.{change.op}", payload, lineage.get()
    )
    row["subject"] = _subject(change.obj)
    row["changed_fields"] = sorted(
        set(values) | set(diff["m2m"]) | set(diff["redacted"]) | set(diff["unknown_old"])
    )
    return row


def _write(session: Session, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    connection = session.connection()
    insert = pg_insert if connection.dialect.name == "postgresql" else sqlite_insert
    statement = (
        insert(Event).on_conflict_do_nothing(index_elements=["dedup_key"]).returning(Event.id)
    )
    inserted = set(connection.execute(statement, rows).scalars())
    deliveries = []
    queues = session.info.setdefault(QUEUES, set())
    for row in rows:
        if row["id"] not in inserted:
            continue
        for subscription in subscriptions():
            if subscription.matches(row):
                deliveries.append(
                    {
                        "id": uuid.uuid4(),
                        "event_id": row["id"],
                        "subscription_id": subscription.name,
                        "queue": subscription.queue,
                        "status": "pending",
                        "max_attempts": subscription.max_attempts,
                        "run_at": row["occurred_at"],
                        "created_at": row["occurred_at"],
                    }
                )
                queues.add(subscription.queue)
    if deliveries:
        connection.execute(sa.insert(Delivery), deliveries)


def _after_commit(session: Session) -> None:
    if not session.in_nested_transaction():
        notify(session.info.pop(QUEUES, set()))


def _transaction_end(session: Session, transaction: Any) -> None:
    if transaction.parent is None:
        for key in (PENDING, DRAFTS_KEY, QUEUES):
            session.info.pop(key, None)
    elif transaction.nested:
        # Never discard a parent's drafts when its savepoint is rolled back.
        session.info[DRAFTS_KEY] = [
            draft
            for draft in session.info.get(DRAFTS_KEY, [])
            if draft.transaction is not transaction
        ]


class BulkWriteNotCapturedError(RuntimeError):
    """Bulk ORM writes bypass event capture."""


def _bulk_guard(state: Any) -> None:
    if (state.is_update or state.is_delete) and any(
        mapper.class_ is Event for mapper in state.all_mappers
    ):
        raise ValueError("Event envelopes are append-only.")
    if not enabled() or settings.EVENTS_BULK_POLICY == "ignore":
        return
    if not (state.is_insert or state.is_update or state.is_delete):
        return
    if not any(get_tracker(mapper.class_) is not None for mapper in state.all_mappers):
        return
    message = "Bulk ORM DML bypasses events; use per-object writes or emit a domain event."
    if settings.EVENTS_BULK_POLICY == "raise":
        raise BulkWriteNotCapturedError(message)
    logger.warning(message)
