from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from obox_builder.conf import settings
from obox_builder.contrib.events import publish
from obox_builder.contrib.events.apps import EventsConfig
from obox_builder.contrib.events.models import Delivery, DeliveryAttempt, Event
from obox_builder.contrib.events.services import discard, redeliver, replay
from obox_builder.contrib.events.worker import Worker, claim, deliver, shutdown
from obox_builder.core.events import on
from obox_builder.core.utils.time import utcnow
from obox_builder.db.session import session_scope
from sqlalchemy.ext.asyncio import AsyncSession

from tests.events import read


async def expire(delivery: Delivery) -> None:
    async with session_scope() as db:
        await db.execute(
            sa.update(Delivery)
            .where(Delivery.id == delivery.id)
            .values(
                locked_until=utcnow() - timedelta(seconds=1), run_at=utcnow() - timedelta(seconds=1)
            )
        )


@pytest.mark.parametrize("fail", [False, True])
async def test_transactional_effect_and_followup_are_atomic(db: AsyncSession, fail: bool) -> None:
    from tests.support.apps.warehouse.models import Shelf

    @on("work.start", name="worker", max_attempts=1)
    async def handler(event: Any, context: Any) -> None:
        context.db.add(Shelf(code="effect"))
        await publish(context.db, "work.done")
        await context.db.flush()
        if fail:
            raise RuntimeError("private-error-content")

    await publish(db, "work.start")
    await db.commit()
    assert await Worker().run_once()
    delivery = await read(sa.select(Delivery))
    assert delivery.status == ("dead" if fail else "succeeded")
    assert await read(sa.select(sa.func.count()).select_from(Shelf)) == (0 if fail else 1)
    done = await read(sa.select(Event).where(Event.type == "work.done"))
    if fail:
        assert done is None
        assert delivery.last_error == "RuntimeError"
    else:
        assert (done.source, done.causation_id, done.hop) == ("job", delivery.event_id, 1)


async def test_retry_idempotency_and_terminal_redelivery(db: AsyncSession) -> None:
    calls: list[str] = []

    @on("external.call", name="external", transactional=False, max_attempts=2)
    async def handler(event: Any, context: Any) -> None:
        assert context.db is None
        calls.append(context.idempotency_key)
        if context.attempt <= 2:
            raise TimeoutError("secret")

    await publish(db, "external.call")
    await db.commit()
    await Worker().run_once()
    delivery = await read(sa.select(Delivery))
    assert delivery.status == "pending"
    assert delivery.attempts == 1
    await expire(delivery)
    await Worker().run_once()
    delivery = await read(sa.select(Delivery))
    assert delivery.status == "dead"
    assert delivery.attempts == 2
    assert calls == [str(delivery.id), str(delivery.id)]
    assert await discard(db, delivery.id)
    assert await redeliver(db, delivery.id)
    await db.commit()
    await Worker().run_once()
    delivery = await read(sa.select(Delivery))
    assert delivery.status == "succeeded"
    assert delivery.attempts == 3
    assert calls[-1] == calls[0]
    assert await read(sa.select(sa.func.count()).select_from(DeliveryAttempt)) == 3


@pytest.mark.parametrize("transactional", [False, True])
async def test_reclaim_fences_stale_worker(db: AsyncSession, transactional: bool) -> None:
    calls: list[int] = []

    @on("claim.work", transactional=transactional)
    async def handler(event: Any, context: Any) -> None:
        calls.append(context.attempt)

    await publish(db, "claim.work")
    await db.commit()
    original = await claim(("default",))
    assert original is not None
    await expire(original)
    replacement = await claim(("default",))
    assert replacement is not None
    assert replacement.id == original.id
    assert replacement.locked_by != original.locked_by
    await deliver(original)
    assert calls == []
    await deliver(replacement)
    assert calls == [2]
    attempt = await read(sa.select(DeliveryAttempt).where(DeliveryAttempt.number == 1))
    assert attempt.outcome == "abandoned"


async def test_competing_claims_and_queue_isolation(db: AsyncSession) -> None:
    @on("claim.work", queue="jobs")
    async def handler(event: Any, context: Any) -> None:
        pass

    for _ in range(2):
        await publish(db, "claim.work")
    await db.commit()
    assert await claim(("default",)) is None
    first, second = await asyncio.gather(claim(("jobs",)), claim(("jobs",)))
    assert first is not None
    assert second is not None
    assert first.id != second.id
    assert await claim(("jobs",)) is None


async def test_expired_final_attempt_becomes_dead(db: AsyncSession) -> None:
    @on("crash.work", max_attempts=1)
    async def handler(event: Any, context: Any) -> None:
        pass

    await publish(db, "crash.work")
    await db.commit()
    delivery = await claim(("default",))
    assert delivery is not None
    await expire(delivery)
    assert await claim(("default",)) is None
    assert (await read(sa.select(Delivery))).status == "dead"
    assert (await read(sa.select(DeliveryAttempt))).outcome == "abandoned"


async def test_self_trigger_guard(db: AsyncSession) -> None:
    @on("loop.work")
    async def handler(event: Any, context: Any) -> None:
        await publish(context.db, "loop.work")

    await publish(db, "loop.work")
    await db.commit()
    await Worker().run_once()
    assert not await Worker().run_once()
    assert await read(sa.select(sa.func.count()).select_from(Event)) == 2


async def test_hop_limit_parks_recursive_handler(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings.load(), "EVENTS_MAX_HOPS", 1)

    @on("loop.work", allow_self=True, max_attempts=1)
    async def handler(event: Any, context: Any) -> None:
        await publish(context.db, "loop.work")

    await publish(db, "loop.work")
    await db.commit()
    await Worker().run_once()
    await Worker().run_once()
    assert (
        await read(sa.select(Delivery).where(Delivery.status == "dead"))
    ).last_error == "ValueError"


async def test_handler_cannot_commit_worker_session(db: AsyncSession) -> None:
    @on("bad.commit", max_attempts=1)
    async def handler(event: Any, context: Any) -> None:
        await context.db.commit()

    await publish(db, "bad.commit")
    await db.commit()
    await Worker().run_once()
    assert (await read(sa.select(Delivery))).last_error == "RuntimeError"


async def test_handler_timeout_becomes_dead(db: AsyncSession) -> None:
    @on("timeout.work", timeout=0.01, max_attempts=1)
    async def handler(event: Any, context: Any) -> None:
        await asyncio.Event().wait()

    await publish(db, "timeout.work")
    await db.commit()
    await Worker().run_once()
    delivery = await read(sa.select(Delivery))
    assert delivery.status == "dead"
    assert delivery.last_error == "TimeoutError"


async def test_replay_finds_matching_events_and_does_not_reset_existing(db: AsyncSession) -> None:
    await publish(db, "noise.first")
    for _ in range(3):
        await publish(db, "replay.match")
    await db.commit()

    @on("replay.*", name="backfill")
    async def handler(event: Any, context: Any) -> None:
        pass

    since, until = utcnow() - timedelta(days=1), utcnow() + timedelta(days=1)
    assert await replay(db, "backfill", since=since, until=until, limit=2) == 2
    await db.commit()
    delivery = await read(sa.select(Delivery).limit(1))
    assert await discard(db, delivery.id)
    await db.commit()
    assert await replay(db, "backfill", since=since, until=until, limit=2) == 1
    await db.commit()
    assert await replay(db, "backfill", since=since, until=until) == 0
    await db.commit()
    assert (await read(sa.select(Delivery).where(Delivery.id == delivery.id))).status == "discarded"


async def test_embedded_worker_wake_and_shutdown(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handled = asyncio.Event()

    @on("wake.now")
    async def handler(event: Any, context: Any) -> None:
        handled.set()

    monkeypatch.setattr(settings.load(), "EVENTS_EMBEDDED_WORKER", True)
    monkeypatch.setattr(settings.load(), "EVENTS_POLL_INTERVAL", 60)
    async with EventsConfig("obox_builder.contrib.events").lifespan():
        await asyncio.sleep(0)
        await publish(db, "wake.now")
        await db.commit()
        await asyncio.wait_for(handled.wait(), 2)
    assert (await read(sa.select(Delivery))).status == "succeeded"


async def test_poll_recovers_lost_wake(db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    handled, scanned = asyncio.Event(), asyncio.Event()

    @on("wake.lost")
    async def handler(event: Any, context: Any) -> None:
        handled.set()

    monkeypatch.setattr("obox_builder.contrib.events.capture.notify", lambda queues: None)
    monkeypatch.setattr(settings.load(), "EVENTS_POLL_INTERVAL", 0.01)
    worker = Worker()
    original = worker.run_once

    async def scan() -> bool:
        result = await original()
        scanned.set()
        return result

    monkeypatch.setattr(worker, "run_once", scan)
    task = asyncio.create_task(worker.run())
    try:
        await asyncio.wait_for(scanned.wait(), 2)
        await publish(db, "wake.lost")
        await db.commit()
        await asyncio.wait_for(handled.wait(), 2)
    finally:
        await shutdown(worker, task)


async def test_shutdown_cancels_handler_and_leaves_recoverable_lease(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()

    @on("work.blocked")
    async def handler(event: Any, context: Any) -> None:
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(settings.load(), "EVENTS_SHUTDOWN_TIMEOUT", 0.01)
    await publish(db, "work.blocked")
    await db.commit()
    worker = Worker()
    task = asyncio.create_task(worker.run())
    try:
        await asyncio.wait_for(started.wait(), 2)
    finally:
        await shutdown(worker, task)
    delivery = await read(sa.select(Delivery))
    assert delivery.status == "running"
    await expire(delivery)
    replacement = await claim(("default",))
    assert replacement is not None
    assert replacement.attempts == 2
