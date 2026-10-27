"""Competing consumers with atomic claims, bounded leases and fenced effects."""

from __future__ import annotations

import asyncio
import logging
import random
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import anyio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from obox_builder.conf import settings
from obox_builder.contrib.events.models import Delivery, DeliveryAttempt, Event
from obox_builder.contrib.events.wake import listen
from obox_builder.contrib.history.context import use
from obox_builder.core.events import Lineage, Subscription, enabled, lineage, subscriptions
from obox_builder.core.exceptions import ImproperlyConfigured
from obox_builder.core.utils.time import utcnow
from obox_builder.db.session import session_scope

logger = logging.getLogger("obox.events")


class LeaseLost(RuntimeError):
    """Another claim owns this delivery; rollback all local effects."""


@dataclass(frozen=True)
class EventMessage:
    id: uuid.UUID
    type: str
    payload: dict[str, Any]
    schema_version: int
    subject: str | None
    occurred_at: datetime
    source: str
    actor_id: int | None
    actor_label: str
    request_id: str | None
    causation_id: uuid.UUID | None
    hop: int

    @classmethod
    def from_event(cls, event: Event) -> EventMessage:
        values = {name: getattr(event, name) for name in cls.__dataclass_fields__}
        if event.occurred_at.tzinfo is None:
            values["occurred_at"] = event.occurred_at.replace(tzinfo=timezone.utc)
        return cls(**values)


@dataclass(frozen=True)
class DeliveryContext:
    delivery_id: uuid.UUID
    idempotency_key: str
    attempt: int
    db: AsyncSession | None


def _fence(delivery: Delivery) -> Any:
    return sa.and_(
        Delivery.id == delivery.id,
        Delivery.status == "running",
        Delivery.locked_by == delivery.locked_by,
        Delivery.locked_until > utcnow(),
    )


async def claim(queues: tuple[str, ...]) -> Delivery | None:
    """One UPDATE ... RETURNING; SQLite serializes writers, PG skips row locks."""
    now = utcnow()
    async with session_scope() as db:
        terminal = list(
            (
                await db.scalars(
                    sa.update(Delivery)
                    .where(
                        Delivery.queue.in_(queues),
                        Delivery.status == "running",
                        Delivery.locked_until <= now,
                        Delivery.attempts >= Delivery.max_attempts,
                    )
                    .values(
                        status="dead",
                        finished_at=now,
                        locked_by=None,
                        locked_until=None,
                        last_error="LeaseExpired",
                    )
                    .returning(Delivery.id)
                )
            ).all()
        )
        if terminal:
            await db.execute(
                sa.update(DeliveryAttempt)
                .where(
                    DeliveryAttempt.delivery_id.in_(terminal), DeliveryAttempt.outcome == "running"
                )
                .values(outcome="abandoned", error="LeaseExpired", finished_at=now)
            )
        due = sa.or_(
            sa.and_(Delivery.status == "pending", Delivery.run_at <= now),
            sa.and_(Delivery.status == "running", Delivery.locked_until <= now),
        )
        candidate = (
            sa.select(Delivery.id)
            .where(Delivery.queue.in_(queues), due, Delivery.attempts < Delivery.max_attempts)
            .order_by(Delivery.run_at, Delivery.id)
            .limit(1)
            .with_for_update(skip_locked=True)
            .scalar_subquery()
        )
        delivery = (
            await db.scalars(
                sa.update(Delivery)
                .where(Delivery.id == candidate)
                .values(
                    status="running",
                    locked_by=uuid.uuid4(),
                    locked_until=now + timedelta(seconds=settings.EVENTS_LEASE_SECONDS),
                    attempts=Delivery.attempts + 1,
                )
                .returning(Delivery)
                .execution_options(synchronize_session=False)
            )
        ).one_or_none()
        if delivery is not None:
            await db.execute(
                sa.update(DeliveryAttempt)
                .where(
                    DeliveryAttempt.delivery_id == delivery.id, DeliveryAttempt.outcome == "running"
                )
                .values(outcome="abandoned", error="LeaseExpired", finished_at=now)
            )
            db.add(
                DeliveryAttempt(
                    delivery_id=delivery.id,
                    number=delivery.attempts,
                    started_at=now,
                    outcome="running",
                )
            )
        return delivery


async def _finish(db: AsyncSession, delivery: Delivery, error: str | None = None) -> None:
    now = utcnow()
    status = (
        "succeeded"
        if error is None
        else ("dead" if delivery.attempts >= delivery.max_attempts else "pending")
    )
    # Full jitter, bounded exponent to avoid overflow after manual retries.
    delay = random.uniform(  # noqa: S311 - scheduling jitter, not cryptography
        0,
        min(
            settings.EVENTS_RETRY_MAX_SECONDS,
            settings.EVENTS_RETRY_BASE_SECONDS * 2 ** min(delivery.attempts - 1, 30),
        ),
    )
    updated = await db.scalar(
        sa.update(Delivery)
        .where(_fence(delivery))
        .values(
            status=status,
            locked_by=None,
            locked_until=None,
            finished_at=now if status != "pending" else None,
            last_error=error,
            run_at=now + timedelta(seconds=delay) if error else now,
        )
        .returning(Delivery.id)
        .execution_options(synchronize_session=False)
    )
    if updated is None:
        raise LeaseLost(str(delivery.id))
    await db.execute(
        sa.update(DeliveryAttempt)
        .where(
            DeliveryAttempt.delivery_id == delivery.id, DeliveryAttempt.number == delivery.attempts
        )
        .values(outcome="succeeded" if error is None else "failed", finished_at=now, error=error)
    )


async def _invoke(
    subscription: Subscription,
    event: Event,
    delivery: Delivery,
    db: AsyncSession | None,
) -> None:
    token = lineage.set(Lineage(event.id, subscription.name, event.hop))
    try:
        async with use(
            source="job",
            actor_id=event.actor_id,
            actor_label=event.actor_label,
            request_id=event.request_id,
        ):
            await subscription.handler(
                EventMessage.from_event(event),
                DeliveryContext(delivery.id, str(delivery.id), delivery.attempts, db),
            )
    finally:
        lineage.reset(token)


async def deliver(delivery: Delivery) -> None:
    subscription = next((s for s in subscriptions() if s.name == delivery.subscription_id), None)
    try:
        if subscription is None:
            raise LookupError("Subscription is not registered on this worker.")
        with anyio.fail_after(subscription.timeout):
            if subscription.transactional:
                async with session_scope() as db:
                    # SQLite has no row locks. Acquire its writer lock before
                    # invoking the handler so a reclaimer cannot overtake it.
                    if db.get_bind().dialect.name == "sqlite":
                        await db.connection(execution_options={"sqlite_begin_immediate": True})
                    owned = await db.scalar(
                        sa.select(Delivery.id).where(_fence(delivery)).with_for_update()
                    )
                    if owned is None:
                        raise LeaseLost(str(delivery.id))
                    event = await db.get(Event, delivery.event_id)
                    if event is None:
                        raise LookupError("Event is missing.")
                    transaction = db.sync_session.get_transaction()
                    db.info["obox.events.handler"] = True
                    try:
                        await _invoke(subscription, event, delivery, db)
                    finally:
                        db.info.pop("obox.events.handler", None)
                    if db.sync_session.get_transaction() is not transaction:
                        raise RuntimeError("Handlers must not commit or rollback their session.")
                    # Flush under lineage so handler data changes retain their
                    # causation and self-trigger guard through the final commit.
                    token = lineage.set(Lineage(event.id, subscription.name, event.hop))
                    try:
                        async with use(
                            source="job",
                            actor_id=event.actor_id,
                            actor_label=event.actor_label,
                            request_id=event.request_id,
                        ):
                            await db.flush()
                            await _finish(db, delivery)
                            await db.commit()
                    finally:
                        lineage.reset(token)
            else:
                async with session_scope() as db:
                    if await db.scalar(sa.select(Delivery.id).where(_fence(delivery))) is None:
                        raise LeaseLost(str(delivery.id))
                    event = await db.get(Event, delivery.event_id)
                if event is None:
                    raise LookupError("Event is missing.")
                await _invoke(subscription, event, delivery, None)
                async with session_scope() as db:
                    await _finish(db, delivery)
        logger.info("Event delivery %s succeeded (attempt %s).", delivery.id, delivery.attempts)
    except LeaseLost:
        logger.warning("Event delivery %s lost its lease; result ignored.", delivery.id)
    except Exception as exc:
        # Exception messages/tracebacks can contain payloads or credentials.
        error = type(exc).__name__[:255]
        try:
            async with session_scope() as db:
                await _finish(db, delivery, error)
        except LeaseLost:
            pass
        logger.warning(
            "Event delivery %s failed: %s (attempt %s).", delivery.id, error, delivery.attempts
        )


class Worker:
    """One in-flight delivery per worker; run multiple processes to scale."""

    def __init__(self, *, queues: tuple[str, ...] = ("default",)) -> None:
        if not queues or not enabled():
            raise ImproperlyConfigured("Install and enable contrib.events and select a queue.")
        for subscription in subscriptions():
            if (
                subscription.queue in queues
                and subscription.timeout >= settings.EVENTS_LEASE_SECONDS
            ):
                raise ImproperlyConfigured(
                    "Handler timeout must be shorter than EVENTS_LEASE_SECONDS."
                )
        self.queues = queues
        self._stopping = False
        self._wake: asyncio.Event | None = None

    async def run_once(self) -> bool:
        delivery = await claim(self.queues)
        if delivery is None:
            return False
        await deliver(delivery)
        return True

    def stop(self) -> None:
        self._stopping = True
        if self._wake is not None:
            self._wake.set()

    async def run(self) -> None:
        with listen(self.queues) as wake:
            self._wake = wake
            try:
                while not self._stopping:
                    wake.clear()
                    try:
                        if await self.run_once():
                            continue
                    except Exception as exc:
                        logger.error(
                            "Event worker database operation failed: %s", type(exc).__name__
                        )
                    with suppress(TimeoutError):
                        await asyncio.wait_for(wake.wait(), timeout=settings.EVENTS_POLL_INTERVAL)
            finally:
                self._wake = None


async def shutdown(worker: Worker, task: asyncio.Task[None]) -> None:
    worker.stop()
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=settings.EVENTS_SHUTDOWN_TIMEOUT)
    except TimeoutError:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
