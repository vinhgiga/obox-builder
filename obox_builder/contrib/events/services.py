"""Explicit, transaction-owned operator actions. No implicit commits."""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from obox_builder.contrib.events.capture import QUEUES
from obox_builder.contrib.events.models import Delivery, Event
from obox_builder.core.events import subscriptions
from obox_builder.core.utils.time import utcnow


async def redeliver(db: AsyncSession, delivery_id: uuid.UUID) -> bool:
    """Requeue a terminal delivery, retaining attempt history and idempotency key."""
    delivery = await db.get(Delivery, delivery_id)
    if delivery is None:
        return False
    subscription = next((s for s in subscriptions() if s.name == delivery.subscription_id), None)
    if subscription is None:
        raise ValueError("Register the subscription before retrying its deliveries.")
    queue = await db.scalar(
        sa.update(Delivery)
        .where(Delivery.id == delivery_id, Delivery.status.in_(("dead", "discarded", "succeeded")))
        .values(
            status="pending",
            run_at=utcnow(),
            finished_at=None,
            last_error=None,
            max_attempts=Delivery.attempts + subscription.max_attempts,
        )
        .returning(Delivery.queue)
    )
    if queue is not None:
        db.info.setdefault(QUEUES, set()).add(queue)
    return queue is not None


async def discard(db: AsyncSession, delivery_id: uuid.UUID) -> bool:
    updated = await db.scalar(
        sa.update(Delivery)
        .where(Delivery.id == delivery_id, Delivery.status.in_(("pending", "dead")))
        .values(status="discarded", finished_at=utcnow())
        .returning(Delivery.id)
    )
    return updated is not None


async def replay(
    db: AsyncSession,
    subscription_id: str,
    *,
    since: datetime,
    until: datetime,
    limit: int = 1000,
) -> int:
    """Fan out missing deliveries over a bounded time window, without id cursors.

    Repeat overlapping windows to include late commits. Existing deliveries,
    including dead/discarded ones, are preserved; redeliver those explicitly.
    """
    subscription = next((s for s in subscriptions() if s.name == subscription_id), None)
    if subscription is None:
        raise ValueError(f"Unknown subscription {subscription_id!r}.")
    if since.tzinfo is None or until.tzinfo is None or since >= until or not 1 <= limit <= 10_000:
        raise ValueError("Use an ordered timezone-aware window and limit 1–10000.")
    missing = ~sa.exists().where(
        Delivery.event_id == Event.id, Delivery.subscription_id == subscription_id
    )
    # Stream the explicit window in bounded batches so nonmatching events
    # cannot starve later matching ones on repeated replay calls.
    events = await db.stream_scalars(
        sa.select(Event)
        .where(Event.occurred_at >= since, Event.occurred_at < until, missing)
        .order_by(Event.occurred_at, Event.id)
        .execution_options(yield_per=500)
    )
    rows = []
    try:
        async for event in events:
            row = {
                "type": event.type,
                "changed_fields": event.changed_fields,
                "source": event.source,
                "producer": event.producer,
            }
            if subscription.matches(row):
                rows.append(
                    {
                        "id": uuid.uuid4(),
                        "event_id": event.id,
                        "subscription_id": subscription_id,
                        "queue": subscription.queue,
                        "max_attempts": subscription.max_attempts,
                        "status": "pending",
                    }
                )
                if len(rows) >= limit:
                    break
    finally:
        await events.close()
    if not rows:
        return 0
    insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
    result = await db.scalars(
        insert(Delivery)
        .on_conflict_do_nothing(index_elements=["event_id", "subscription_id"])
        .returning(Delivery.id),
        rows,
    )
    count = len(result.all())
    if count:
        db.info.setdefault(QUEUES, set()).add(subscription.queue)
    return count
