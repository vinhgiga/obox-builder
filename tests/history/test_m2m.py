"""M2M capture: relation adds/removes recorded as id lists in the payload."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from tests.history.test_capture import changes_for
from tests.support.apps.warehouse.models import Item, Tag


async def make_item_with_tags(db: AsyncSession) -> tuple[Item, Tag, Tag]:
    first = Tag(label="fragile")
    second = Tag(label="heavy")
    db.add_all([first, second])
    await db.commit()
    item = Item(name="Anvil", sku="ANV-1", tags=[first])
    db.add(item)
    await db.commit()
    return item, first, second


async def test_create_with_relations_records_added_ids(db: AsyncSession) -> None:
    item, first, _second = await make_item_with_tags(db)

    [row] = await changes_for(db, item, "create")
    assert row.changes["m2m"] == {"tags": {"add": [str(first.id)], "remove": []}}


async def test_update_records_add_and_remove(db: AsyncSession) -> None:
    item, first, second = await make_item_with_tags(db)

    item.tags.remove(first)
    item.tags.append(second)
    await db.commit()

    [row] = await changes_for(db, item, "update")
    assert row.changes["fields"] == {}
    assert row.changes["m2m"] == {"tags": {"add": [str(second.id)], "remove": [str(first.id)]}}


async def test_new_related_row_gets_its_pk_resolved(db: AsyncSession) -> None:
    item, _first, _second = await make_item_with_tags(db)

    fresh = Tag(label="just-created")
    item.tags.append(fresh)
    await db.commit()

    [row] = await changes_for(db, item, "update")
    assert row.changes["m2m"]["tags"]["add"] == [str(fresh.id)]
    assert fresh.id is not None


async def test_remove_then_readd_same_row_is_a_noop(db: AsyncSession) -> None:
    item, first, _second = await make_item_with_tags(db)
    item.tags.remove(first)
    item.tags.append(first)
    await db.commit()

    assert await changes_for(db, item, "update") == []


async def test_delete_snapshots_loaded_collection_as_removed(db: AsyncSession) -> None:
    item, first, _second = await make_item_with_tags(db)

    await db.delete(item)
    await db.commit()

    [row] = await changes_for(db, item, "delete")
    assert row.changes["m2m"] == {"tags": {"add": [], "remove": [str(first.id)]}}


async def test_delete_without_loaded_collection_records_no_m2m(db: AsyncSession) -> None:
    item, _first, _second = await make_item_with_tags(db)

    db.expire(item)
    await db.delete(item)
    await db.commit()

    [row] = await changes_for(db, item, "delete")
    assert row.changes["m2m"] == {}


async def test_m2m_only_change_still_writes_an_entry(db: AsyncSession) -> None:
    item, _first, second = await make_item_with_tags(db)
    item.tags.append(second)
    await db.commit()

    rows = await changes_for(db, item, "update")
    assert len(rows) == 1
    assert rows[0].changes["m2m"]["tags"]["add"] == [str(second.id)]


async def test_ids_are_sorted(db: AsyncSession) -> None:
    tags = [Tag(label=f"t-{index}") for index in range(4)]
    db.add_all(tags)
    await db.commit()
    item = Item(name="Anvil", sku="ANV-1", tags=[tags[0], tags[1]])
    db.add(item)
    await db.commit()

    item.tags.remove(tags[1])
    item.tags.remove(tags[0])
    item.tags.append(tags[3])
    item.tags.append(tags[2])
    await db.commit()

    [row] = await changes_for(db, item, "update")
    sides = row.changes["m2m"]["tags"]
    assert sides["add"] == sorted(str(tag.id) for tag in tags[2:])
    assert sides["remove"] == sorted(str(tag.id) for tag in tags[:2])


async def test_service_entry_exposes_relation_diffs(db: AsyncSession) -> None:
    from obox_builder.contrib.history.service import history_entries

    item, first, second = await make_item_with_tags(db)
    item.tags.remove(first)
    item.tags.append(second)
    await db.commit()

    [entry] = await history_entries(db, Item, item.id, limit=1)
    [relation] = entry.m2m
    assert relation.name == "tags"
    assert relation.label == "Tags"
    assert relation.added == (str(second.id),)
    assert relation.removed == (str(first.id),)


async def test_update_without_relation_changes_keeps_m2m_empty(db: AsyncSession) -> None:
    item, *_ = await make_item_with_tags(db)
    await db.execute(sa.select(Item).options(selectinload(Item.tags)).where(Item.id == item.id))
    item.quantity = 9
    await db.commit()

    [row] = await changes_for(db, item, "update")
    assert row.changes["m2m"] == {}
    assert row.changes["fields"] == {"quantity": [0, 9]}
