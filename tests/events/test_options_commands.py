from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from obox_builder.conf import settings
from obox_builder.contrib.events import publish
from obox_builder.contrib.events.checks import check_events
from obox_builder.contrib.events.management.commands import (
    events_discard,
    events_emit,
    events_replay,
    events_retry,
    events_status,
    events_worker,
)
from obox_builder.contrib.events.models import Delivery, Event
from obox_builder.contrib.events.options import parse_events
from obox_builder.contrib.events.services import replay
from obox_builder.contrib.events.worker import Worker
from obox_builder.core.events import emit, on
from obox_builder.core.exceptions import ImproperlyConfigured
from obox_builder.core.management.base import CommandError
from obox_builder.core.utils.time import utcnow
from sqlalchemy.ext.asyncio import AsyncSession

from tests.events import read


@pytest.mark.parametrize("raw", [False, None, True, {}])
def test_declarations(events_env: None, raw: Any) -> None:
    model = type("Declaration", (), {"__events__": raw, "__history__": None})
    assert parse_events(model).history.enabled == (raw is not False and raw is not None)


@pytest.mark.parametrize(
    "raw",
    [
        "bad",
        1,
        {"unknown": True},
        {"previous": 1},
        {"fields": "name"},
        {"mask": "email"},
        {"relations": "tags"},
    ],
)
def test_invalid_declarations(events_env: None, raw: Any) -> None:
    with pytest.raises(ImproperlyConfigured):
        parse_events(type("Declaration", (), {"__events__": raw, "__history__": None}))


@pytest.mark.parametrize("payload", [{"bad": float("nan")}, {"bad": object()}, [1]])
async def test_invalid_payload_is_not_staged(db: AsyncSession, payload: Any) -> None:
    with pytest.raises((TypeError, ValueError)):
        await publish(db, "invalid.payload", payload)
    await db.commit()
    assert await read(sa.select(sa.func.count()).select_from(Event)) == 0


@pytest.mark.parametrize(
    "options",
    [
        {"type": "bad type"},
        {"type": ""},
        {"schema_version": 0},
        {"dedup_key": ""},
        {"dedup_key": "x" * 256},
    ],
)
async def test_invalid_envelope(db: AsyncSession, options: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="Event type|schema_version|dedup_key"):
        await publish(db, **{"type": "valid.type", **options})


async def test_payload_limit_and_disabled_noop(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.load(), "EVENTS_MAX_PAYLOAD_BYTES", 4)
    with pytest.raises(ValueError, match="MAX_PAYLOAD"):
        await publish(db, "large", {"long": "value"})
    monkeypatch.setattr(settings.load(), "EVENTS_ENABLED", False)
    assert emit(db, "invalid type", {"bad": object()}) is None
    with pytest.raises(ImproperlyConfigured):
        Worker()


def test_handlers_reject_sync_duplicate_and_invalid_options(events_env: None) -> None:
    with pytest.raises(ImproperlyConfigured):
        on("valid")(lambda event, context: None)
    with pytest.raises(ImproperlyConfigured):
        on("valid", queue="bad queue")
    with pytest.raises(ImproperlyConfigured):
        on("valid", max_attempts=0)
    for timeout in (0, float("nan")):
        with pytest.raises(ImproperlyConfigured):
            on("valid", timeout=timeout)

    @on("valid", name="unique")
    async def first(event: Any, context: Any) -> None:
        pass

    with pytest.raises(ImproperlyConfigured, match="Duplicate"):
        on("valid", name="unique")(test_invalid_payload_is_not_staged)


@pytest.mark.parametrize(
    "raw,code",
    [
        ({"unknown": 1}, "E401"),
        ({"fields": ["missing"]}, "E403"),
        ({"relations": ["shelf"]}, "E404"),
    ],
)
def test_model_checks(
    events_env: None, monkeypatch: pytest.MonkeyPatch, raw: Any, code: str
) -> None:
    from tests.support.apps.warehouse.models import Item

    monkeypatch.setattr(Item, "__events__", raw)
    assert f"obox.events.{code}" in {message.id for message in check_events()}


def test_timeout_lease_check(events_env: None) -> None:
    @on("valid", timeout=settings.EVENTS_LEASE_SECONDS)
    async def handler(event: Any, context: Any) -> None:
        pass

    assert "obox.events.E405" in {message.id for message in check_events()}
    with pytest.raises(ImproperlyConfigured, match="shorter"):
        Worker()


def test_checks_before_app_setup(events_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from obox_builder.apps import apps

    monkeypatch.setattr(apps, "models_ready", False)
    assert check_events() == []


async def test_replay_rejects_invalid_windows_and_subscription(db: AsyncSession) -> None:
    @on("valid", name="valid")
    async def handler(event: Any, context: Any) -> None:
        pass

    now = utcnow()
    for name, since, until, limit in (
        ("missing", now, now + timedelta(seconds=1), 1),
        ("valid", now, now, 1),
        ("valid", now.replace(tzinfo=None), now + timedelta(seconds=1), 1),
        ("valid", now, now + timedelta(seconds=1), 0),
    ):
        with pytest.raises(ValueError, match="Unknown|window"):
            await replay(db, name, since=since, until=until, limit=limit)


async def test_operator_commands(db: AsyncSession, capsys: pytest.CaptureFixture[str]) -> None:
    @on("cli.*", name="cli.handler", max_attempts=1)
    async def handler(event: Any, context: Any) -> None:
        pass

    await events_emit.handle.func(
        type="cli.event", payload='{"ok":true}', subject="cli:1", dedup_key=None
    )
    delivery = await read(sa.select(Delivery))
    assert (await read(sa.select(Event))).source == "cli"
    await events_discard.handle.func(delivery.id)
    await events_retry.handle.func(delivery.id)
    await events_worker.handle.func(queue=["default"], once=True)
    await events_status.handle.func()
    await events_replay.handle.func(
        subscription="cli.handler",
        since=(utcnow() - timedelta(days=1)).isoformat(),
        until=(utcnow() + timedelta(days=1)).isoformat(),
        limit=1,
    )
    assert (await read(sa.select(Delivery))).status == "succeeded"
    output = capsys.readouterr().out
    assert "succeeded = 1" in output
    assert "Created 0 missing deliveries" in output
    assert "Requeued" in output
    assert "Discarded" in output


@pytest.mark.parametrize("payload", ["null", "[]", "not-json"])
async def test_emit_command_rejects_invalid_json(db: AsyncSession, payload: str) -> None:
    with pytest.raises(CommandError):
        await events_emit.handle.func(
            type="cli.event", payload=payload, subject=None, dedup_key=None
        )


@pytest.mark.parametrize("command", [events_retry, events_discard])
async def test_operator_commands_report_missing_delivery(db: AsyncSession, command: Any) -> None:
    with pytest.raises(CommandError, match="does not exist"):
        await command.handle.func(uuid.uuid4())
