from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa
from obox_builder.conf import settings
from obox_builder.contrib.events import publish
from obox_builder.contrib.events.capture import BulkWriteNotCapturedError
from obox_builder.contrib.events.models import Delivery, Event
from obox_builder.contrib.history.context import use
from obox_builder.core.events import emit, on
from sqlalchemy.ext.asyncio import AsyncSession

from tests.events import read


async def test_clean_publish_snapshot_context_and_dedup(db: AsyncSession) -> None:
    @on("shop.*", name="shop.handler")
    async def handler(event: Any, context: Any) -> None:
        pass

    payload = {"items": [1]}
    async with use(source="api", actor_id=7, actor_label="operator", request_id="req-1"):
        proposed = await publish(db, "shop.created", payload, dedup_key="shop:1", schema_version=2)
        payload["items"].append(2)
        await db.commit()
    await publish(db, "shop.created", {"duplicate": True}, dedup_key="shop:1")
    await db.commit()
    event = await read(sa.select(Event))
    assert (event.id, event.payload, event.schema_version) == (proposed, {"items": [1]}, 2)
    assert (event.source, event.actor_id, event.actor_label, event.request_id) == (
        "api",
        7,
        "operator",
        "req-1",
    )
    assert await read(sa.select(sa.func.count()).select_from(Delivery)) == 1


@pytest.mark.parametrize("flush", [False, True])
async def test_rollback_removes_data_event_and_delivery(db: AsyncSession, flush: bool) -> None:
    from tests.support.apps.warehouse.models import Item

    @on("warehouse.*")
    async def handler(event: Any, context: Any) -> None:
        pass

    db.add(Item(name="rolled back", sku="R"))
    await publish(db, "domain.rollback")
    if flush:
        await db.flush()
    await db.rollback()
    for model in (Item, Event, Delivery):
        assert await read(sa.select(sa.func.count()).select_from(model)) == 0


@pytest.mark.parametrize("inner_commit,outer_commit", [(False, True), (True, False), (True, True)])
async def test_savepoints_preserve_only_committed_drafts(
    db: AsyncSession,
    inner_commit: bool,
    outer_commit: bool,
) -> None:
    await publish(db, "domain.outer")
    nested = await db.begin_nested()
    await publish(db, "domain.inner")
    if inner_commit:
        await nested.commit()
    else:
        await nested.rollback()
    if outer_commit:
        await db.commit()
    else:
        await db.rollback()
    expected = (1 + inner_commit) if outer_commit else 0
    assert await read(sa.select(sa.func.count()).select_from(Event)) == expected


async def test_subject_gets_generated_pk_and_noop_update_is_skipped(db: AsyncSession) -> None:
    from tests.support.apps.warehouse.models import Item

    item = Item(name="same", sku="S")
    db.add(item)
    proposed = emit(db.sync_session, "domain.subject", subject=item)
    await db.commit()
    item.name = "same"
    await db.commit()
    event = await read(sa.select(Event).where(Event.id == proposed))
    assert event.subject == f"warehouse.item:{item.id}"
    assert await read(sa.select(sa.func.count()).select_from(Event)) == 2


async def test_capture_privacy_updates_and_deletion(db: AsyncSession) -> None:
    from tests.support.apps.warehouse.models import Contact, Item

    contact = Contact(name="A", email="alice@example.com")
    item = Item(name="before", sku="P", secret_token="private-token", internal_note="hidden")
    db.add_all([contact, item])
    await db.commit()
    created = await read(sa.select(Event).where(Event.type == "warehouse.contact.create"))
    assert created.payload["fields"]["email"] == "a***@example.com"
    item_event = await read(sa.select(Event).where(Event.type == "warehouse.item.create"))
    assert "private-token" not in str(item_event.payload)
    assert "hidden" not in str(item_event.payload)
    assert "secret_token" in item_event.payload["redacted"]
    item.name = "after"
    await db.commit()
    updated = await read(sa.select(Event).where(Event.type == "warehouse.item.update"))
    assert updated.payload["fields"]["name"] == "after"
    assert "previous" not in updated.payload
    await db.delete(item)
    await db.commit()
    deleted = await read(sa.select(Event).where(Event.type == "warehouse.item.delete"))
    assert deleted.subject == f"warehouse.item:{item.id}"


async def test_m2m_and_previous_values(db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.support.apps.warehouse.models import Item, Tag

    monkeypatch.setattr(Item, "__events__", {"relations": ["tags"], "previous": True})
    tag = Tag(label="first")
    item = Item(name="old", sku="M", tags=[tag])
    db.add(item)
    await db.commit()
    event = await read(sa.select(Event).where(Event.type == "warehouse.item.create"))
    assert event.payload["m2m"]["tags"]["add"] == [str(tag.id)]
    await db.refresh(item, ["tags"])
    item.tags.clear()
    item.name = "new"
    await db.commit()
    event = await read(sa.select(Event).where(Event.type == "warehouse.item.update"))
    assert event.payload["m2m"]["tags"]["remove"] == [str(tag.id)]
    assert event.payload["previous"]["name"] == "old"


@pytest.mark.parametrize("policy", ["raise", "warn", "ignore"])
async def test_bulk_guard(db: AsyncSession, monkeypatch: pytest.MonkeyPatch, policy: str) -> None:
    from tests.support.apps.warehouse.models import Item

    monkeypatch.setattr(settings.load(), "EVENTS_BULK_POLICY", policy)
    statement = sa.update(Item).values(name="bulk")
    if policy == "raise":
        with pytest.raises(BulkWriteNotCapturedError):
            await db.execute(statement)
    else:
        await db.execute(statement)
    await db.rollback()


@pytest.mark.parametrize("bulk", [False, True])
async def test_envelopes_are_append_only(db: AsyncSession, bulk: bool) -> None:
    event_id = await publish(db, "domain.immutable")
    await db.commit()
    with pytest.raises(ValueError, match="append-only"):
        if bulk:
            await db.execute(sa.update(Event).values(type="changed"))
        else:
            event = await db.get(Event, event_id)
            assert event is not None
            event.type = "changed"
            await db.commit()
    await db.rollback()


async def test_filters_use_pattern_fields_and_source(db: AsyncSession) -> None:
    from tests.support.apps.warehouse.models import Item

    @on("warehouse.item.*", name="selected", watch_fields=("name",), origins=("api",))
    async def selected(event: Any, context: Any) -> None:
        pass

    @on("unrelated.*", name="unrelated")
    async def unrelated(event: Any, context: Any) -> None:
        pass

    async with use(source="api"):
        item = Item(name="first", sku="F")
        db.add(item)
        await db.commit()
        item.quantity = 2
        await db.commit()
    item.name = "system"
    await db.commit()
    assert await read(sa.select(sa.func.count()).select_from(Delivery)) == 1
