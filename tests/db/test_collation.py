from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
import sqlalchemy as sa
from obox_builder.core.exceptions import ImproperlyConfigured
from obox_builder.db import collation
from obox_builder.fields import FieldSpec, QuerySpec, field_types
from sqlalchemy import event
from sqlalchemy.dialects import sqlite
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

ALPHABET = [
    "a",
    "ă",
    "â",
    "b",
    "c",
    "d",
    "đ",
    "e",
    "ê",
    "f",
    "g",
    "h",
    "i",
    "j",
    "k",
    "l",
    "m",
    "n",
    "o",
    "ô",
    "ơ",
    "p",
    "q",
    "r",
    "s",
    "t",
    "u",
    "ư",
    "v",
    "w",
    "x",
    "y",
    "z",
]
TONES = ["a", "A", "à", "À", "ả", "Ả", "ã", "Ã", "á", "Á", "ạ", "Ạ"]


@pytest.fixture
async def connection() -> AsyncIterator[AsyncConnection]:
    url = os.environ.get("OBOX_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    engine = create_async_engine(url)
    if engine.dialect.name == "sqlite":
        pytest.importorskip("icu", reason="requires the vietnamese extra")

        @event.listens_for(engine.sync_engine, "connect")
        def configure(dbapi_connection: Any, _record: Any) -> None:
            dbapi_connection.create_function("obox_vi_sort_key", 1, collation.sqlite_sort_key())

    try:
        async with engine.connect() as conn:
            yield conn
            await conn.rollback()
    finally:
        await engine.dispose()


def specs_for(table: sa.Table) -> dict[str, FieldSpec]:
    return {
        column.name: FieldSpec(
            name=column.name,
            label=column.name,
            column=column,
            primary_key=column.primary_key,
            type=field_types.create("text" if isinstance(column.type, sa.String) else "integer"),
        )
        for column in table.c
    }


@pytest.mark.parametrize(
    "values", [ALPHABET, TONES, ["áa", "ab", "ăa", "âa", "bà", "baa", "bàb", "dân", "đàn", "em"]]
)
@pytest.mark.parametrize("descending", [False, True])
async def test_icu_vietnamese_order_before_pagination(
    connection: AsyncConnection,
    values: list[str],
    descending: bool,
) -> None:
    metadata = sa.MetaData()
    table = sa.Table(
        "test_vi_order",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String),
    )
    await connection.run_sync(metadata.create_all)
    await connection.execute(
        table.insert(),
        [{"id": index, "name": name} for index, name in enumerate(reversed(values), 1)]
        + [{"id": len(values) + 1, "name": None}],
    )
    specs = specs_for(table)
    query = QuerySpec.parse({"sort": "-name" if descending else "name"}, specs)
    stmt = query.apply(sa.select(table.c.name), specs, text_collation="vi")
    result = (await connection.scalars(stmt)).all()
    expected = list(reversed(values)) if descending else values
    assert result == [*expected, None]
    paged: list[str | None] = []
    for offset in range(0, len(result), 3):
        paged.extend((await connection.scalars(stmt.limit(3).offset(offset))).all())
    assert paged == result
    await connection.run_sync(metadata.drop_all)
    await connection.commit()


async def test_normalization_ties_groups_and_numeric_secondary_order(
    connection: AsyncConnection,
) -> None:
    metadata = sa.MetaData()
    table = sa.Table(
        "test_vi_ties",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String),
        sa.Column("rank", sa.Integer),
    )
    await connection.run_sync(metadata.create_all)
    decomposed = "a\u0306\u0301"
    await connection.execute(
        table.insert(),
        [
            {"id": 1, "name": "ắ", "rank": 2},
            {"id": 2, "name": decomposed, "rank": 10},
            {"id": 3, "name": "ắ", "rank": 10},
            {"id": 4, "name": decomposed, "rank": 2},
            {"id": 5, "name": "ắ", "rank": 2},
        ],
    )
    specs = specs_for(table)
    query = QuerySpec.parse({"group": "name", "sort": "rank"}, specs)
    stmt = query.apply(sa.select(table.c.id), specs, text_collation="vi")
    assert (await connection.scalars(stmt)).all() == [4, 2, 5, 1, 3]
    grouped = QuerySpec.parse({"group": "-name", "sort": "-rank"}, specs)
    descending = grouped.apply(sa.select(table.c.id), specs, text_collation="vi")
    assert (await connection.scalars(descending)).all() == [3, 5, 1, 2, 4]
    await connection.run_sync(metadata.drop_all)
    await connection.commit()


async def test_native_enum_uses_vietnamese_text_order(connection: AsyncConnection) -> None:
    metadata = sa.MetaData()
    table = sa.Table(
        "test_vi_enum_order",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.Enum("Em", "Đa", "Da", "Ba", "Ân", "Ăn", "An", name="test_vi_labels")),
    )
    await connection.run_sync(metadata.create_all)
    values = ["An", "Ăn", "Ân", "Ba", "Da", "Đa", "Em"]
    await connection.execute(
        table.insert(),
        [{"id": index, "name": name} for index, name in enumerate(reversed(values), 1)],
    )
    specs = specs_for(table)
    query = QuerySpec.parse({"sort": "name"}, specs)
    stmt = query.apply(sa.select(table.c.name), specs, text_collation="vi")
    assert (await connection.scalars(stmt)).all() == values
    await connection.run_sync(metadata.drop_all)
    await connection.commit()


def test_missing_pyicu_fails_with_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(_name: str) -> Any:
        raise ImportError("not installed")

    monkeypatch.setattr(collation, "import_module", missing)
    stmt = sa.select(sa.literal("a")).order_by(collation.VietnameseOrder(sa.literal("a")))
    with pytest.raises(ImproperlyConfigured, match=r"obox-builder\[vietnamese\]"):
        stmt.compile(dialect=sqlite.dialect())
    assert collation.sqlite_sort_key()(None) is None
