"""VersionedMixin: optimistic locking and ``row_version`` capture (Phase 3)."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from tests.history.test_capture import changes_for
from tests.support.apps.warehouse.models import Contact, Item


async def test_version_defaults_and_bumps(db: AsyncSession) -> None:
    item = Item(name="Anvil", sku="ANV-1", quantity=1)
    db.add(item)
    await db.commit()
    assert item.version == 1

    item.quantity = 2
    await db.commit()
    assert item.version == 2

    item.quantity = 3
    await db.commit()
    assert item.version == 3


async def test_row_version_recorded_per_entry(db: AsyncSession) -> None:
    item = Item(name="Anvil", sku="ANV-1", quantity=1)
    db.add(item)
    await db.commit()
    item.quantity = 2
    await db.commit()

    create, update = [row for row in await changes_for(db, item) if row.op in ("create", "update")]
    assert create.row_version == 1
    assert update.row_version == 2
    assert "version" not in update.changes["fields"]


async def test_stale_instance_raises_stale_data_error(db: AsyncSession) -> None:
    from obox_builder.db import get_sessionmaker

    item = Item(name="Anvil", sku="ANV-1", quantity=1)
    db.add(item)
    await db.commit()
    item_id = item.id

    maker = get_sessionmaker()
    async with maker() as first, maker() as second:
        one = await first.get(Item, item_id)
        two = await second.get(Item, item_id)
        assert one is not None
        assert two is not None
        one.quantity = 10
        await first.flush()
        await first.commit()  # release the row lock; the version bump is the guard
        two.quantity = 20
        with pytest.raises(StaleDataError):
            await second.flush()


async def test_unversioned_model_records_null_row_version(db: AsyncSession) -> None:
    from obox_builder.contrib.history import track

    from tests.support.apps.warehouse.models import Crate

    track(Crate)
    try:
        crate = Crate(code="CRT-9", weight_grams=5)
        db.add(crate)
        await db.commit()
        crate.weight_grams = 6
        await db.commit()

        [row] = [r for r in await changes_for(db, crate) if r.op == "update"]
        assert row.row_version is None
    finally:
        from obox_builder.contrib.history.tracker import _trackers

        _trackers.pop(Crate, None)


async def test_masked_field_records_masked_payload(db: AsyncSession) -> None:
    contact = Contact(name="Ada", email="ada@example.com")
    db.add(contact)
    await db.commit()
    contact.email = "ada.lovelace@example.com"
    await db.commit()

    [row] = [r for r in await changes_for(db, contact) if r.op == "update"]
    assert row.changes["fields"] == {}
    assert row.changes["masked"] == {
        "email": ["a***@example.com", "a***@example.com"],
    }
