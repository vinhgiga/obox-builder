"""``history.export``: keyset streaming, NDJSON payloads, CSV guard."""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from obox_builder.contrib.history.export import (
    CSV_COLUMNS,
    csv_header,
    csv_line,
    export_entries,
    guard_cell,
    ndjson_line,
)
from obox_builder.contrib.history.models import RowChange
from obox_builder.contrib.history.service import FeedFilters, model_label
from obox_builder.core.utils.time import utcnow
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.apps.warehouse.models import Item, Shelf


async def _seed(db: AsyncSession, count: int = 2) -> list[Item]:
    from obox_builder.contrib.history.context import use

    items = []
    async with use(actor_id=7, actor_label="alice"):
        for index in range(count):
            item = Item(name=f"Item {index}", sku=f"SKU-{index}")
            db.add(item)
            await db.commit()
            items.append(item)
    return items


async def _export(db: AsyncSession, filters: FeedFilters | None = None, **kwargs: object):
    return [entry async for entry in export_entries(db, filters, **kwargs)]  # type: ignore[arg-type]


async def test_export_streams_everything_in_id_order(db: AsyncSession) -> None:
    items = await _seed(db, count=3)

    entries = await _export(db, batch_size=1)

    assert [entry.object_pk for entry in entries] == [str(item.id) for item in items]
    assert [entry.id for entry in entries] == sorted(entry.id for entry in entries)


async def test_export_filters_model_window_and_actor(db: AsyncSession) -> None:
    items = await _seed(db, count=2)
    shelf = Shelf(code="RACK-1")
    db.add(shelf)
    await db.commit()

    labels = frozenset({model_label(Item)})
    by_model = await _export(db, FeedFilters(model_labels=labels))
    assert {entry.object_pk for entry in by_model} == {str(item.id) for item in items}

    now = utcnow()
    windowed = await _export(
        db,
        FeedFilters(since=now - timedelta(minutes=5), until=now + timedelta(minutes=5)),
        actor_id=7,
    )
    assert len(windowed) == 2

    assert await _export(db, FeedFilters(actor="nobody")) == []
    assert await _export(db, FeedFilters(model_labels=frozenset())) == []


async def test_ndjson_line_is_json_with_all_columns(db: AsyncSession) -> None:
    [item] = await _seed(db, count=1)

    [entry] = await _export(db)
    payload = json.loads(ndjson_line(entry))

    assert payload["model_label"] == "warehouse.item"
    assert payload["object_pk"] == str(item.id)
    assert payload["op"] == "create"
    assert payload["actor_id"] == 7
    assert payload["actor_label"] == "alice"
    assert payload["changes"]["fields"]["name"] == [None, "Item 0"]
    assert isinstance(payload["occurred_at"], str)


async def test_csv_lines_parse_and_cover_all_columns(db: AsyncSession) -> None:
    await _seed(db, count=1)
    [entry] = await _export(db)

    header = list(csv.reader(io.StringIO(csv_header())))
    assert header == [list(CSV_COLUMNS)]
    [row] = list(csv.reader(io.StringIO(csv_line(entry))))
    assert len(row) == len(CSV_COLUMNS)
    assert row[CSV_COLUMNS.index("changes")].startswith("{")
    assert row[CSV_COLUMNS.index("occurred_at")] == entry.occurred_at.isoformat(sep=" ")


async def test_csv_export_guards_formula_injection(db: AsyncSession) -> None:
    item = Item(name='=HYPERLINK("http://evil")', sku="SKU-EVIL")
    db.add(item)
    await db.commit()

    [entry] = await _export(db)
    [row] = list(csv.reader(io.StringIO(csv_line(entry))))
    assert row[CSV_COLUMNS.index("object_repr")].startswith("'=HYPERLINK")


def test_guard_cell_only_touches_formula_starters() -> None:
    assert guard_cell("=1+1") == "'=1+1"
    assert guard_cell("+cmd") == "'+cmd"
    assert guard_cell("-2") == "'-2"
    assert guard_cell("@import") == "'@import"
    assert guard_cell("plain") == "plain"
    assert guard_cell("'quoted") == "'quoted"


async def test_export_window_is_exclusive_upper_bound(db: AsyncSession) -> None:
    await _seed(db, count=2)
    boundary = datetime(2026, 1, 1, tzinfo=timezone.utc)
    await db.execute(sa.update(RowChange).values(occurred_at=boundary))
    await db.commit()

    inside = await _export(
        db,
        FeedFilters(
            since=boundary - timedelta(seconds=1),
            until=boundary + timedelta(seconds=1),
        ),
    )
    outside = await _export(db, FeedFilters(until=boundary))

    assert len(inside) == 2
    assert outside == []
