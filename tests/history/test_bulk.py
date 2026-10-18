"""``history.bulk_update()``: audit-safe bulk updates through the unit of work."""

from __future__ import annotations

import sqlalchemy as sa
from obox_builder.contrib.history import bulk_update
from sqlalchemy.ext.asyncio import AsyncSession

from tests.history.test_capture import changes_for
from tests.support.apps.warehouse.models import Item


async def _items(db: AsyncSession, count: int = 3) -> list[Item]:
    items = [Item(name=f"Item {n}", sku=f"SKU-{n}", quantity=n) for n in range(count)]
    db.add_all(items)
    await db.commit()
    return items


async def test_bulk_update_records_one_entry_per_row(db: AsyncSession) -> None:
    items = await _items(db)

    visited = await bulk_update(db, Item, {"is_active": False}, where=Item.quantity >= 1)
    await db.commit()

    assert visited == 2
    untouched = await changes_for(db, items[0], "update")
    assert untouched == []
    for item in items[1:]:
        [row] = await changes_for(db, item, "update")
        assert row.changes["fields"] == {"is_active": [True, False]}
        assert row.source == "system"


async def test_bulk_update_without_where_touches_all_rows(db: AsyncSession) -> None:
    items = await _items(db)

    visited = await bulk_update(db, Item, {"quantity": 9})
    await db.commit()

    assert visited == 3
    for item in items:
        [row] = await changes_for(db, item, "update")
        assert row.changes["fields"]["quantity"][1] == 9


async def test_bulk_update_shares_one_change_set_and_skips_no_ops(db: AsyncSession) -> None:
    items = await _items(db)

    visited = await bulk_update(db, Item, {"name": items[0].name})
    await db.commit()

    assert visited == 3
    entries = [await changes_for(db, item, "update") for item in items]
    assert [len(found) for found in entries] == [0, 1, 1]
    assert entries[1][0].change_set_id == entries[2][0].change_set_id


async def test_bulk_update_returns_zero_when_nothing_matches(db: AsyncSession) -> None:
    await _items(db)

    assert await bulk_update(db, Item, {"is_active": False}, where=sa.false()) == 0
