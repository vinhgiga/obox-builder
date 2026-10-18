"""Write-path cost: the +1 INSERT invariant (always) and NF2 benchmarks (marked).

The invariant test asserts that a flush writes one history ``executemany``, not
one statement per changed row. The wall-clock benchmarks are excluded from the
default run (``-m "not benchmark"`` in ``addopts``); run them with
``uv run pytest tests/history/test_write_cost.py -m benchmark -s``.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator, Sequence
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.apps.warehouse.models import Crate, Item

BENCH_SIZE = 500
BENCH_REPEATS = 3


@pytest.fixture
def statements(db: AsyncSession) -> Iterator[list[str]]:
    """Every SQL statement the session executes, in order."""
    collected: list[str] = []
    sync_engine = db.get_bind()

    def record(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        collected.append(" ".join(statement.split()))

    event.listen(sync_engine, "before_cursor_execute", record)
    yield collected
    event.remove(sync_engine, "before_cursor_execute", record)


def _history_inserts(statements: Sequence[str]) -> list[str]:
    return [
        sql for sql in statements if sql.casefold().startswith("insert into history_row_change")
    ]


async def test_flush_adds_exactly_one_history_insert(
    db: AsyncSession, statements: list[str]
) -> None:
    db.add_all(Item(name=f"Anvil {index}", sku=f"SKU-{index}") for index in range(5))
    await db.flush()
    assert len(_history_inserts(statements)) == 1

    statements.clear()
    rows = list(await db.scalars(sa.select(Item)))
    for row in rows:
        row.quantity += 1
    await db.flush()
    assert len(_history_inserts(statements)) == 1


# -- NF2 benchmarks (opt-in) --------------------------------------------------
# Control = the same ORM flush without history: ``Crate`` (untracked) vs
# ``Item`` (tracked). The measured cost is the tracker diff + payload build +
# the single history executemany; the ORM flush cost is common to both.


@pytest.mark.benchmark
async def test_benchmark_write_overhead(db: AsyncSession, override_settings: Any) -> None:
    from obox_builder.contrib.history.models import RowChange
    from obox_builder.db import get_sessionmaker

    override_settings(HISTORY_BULK_POLICY="ignore")
    sessionmaker = get_sessionmaker()
    size, repeats = BENCH_SIZE, BENCH_REPEATS

    async def clear() -> None:
        async with sessionmaker() as session:
            await session.execute(sa.delete(Item))
            await session.execute(sa.delete(Crate))
            await session.commit()

    async def seed_items() -> None:
        async with sessionmaker() as session:
            session.add_all(
                Item(name=f"Tracked {index}", sku=f"TRK-{index}", notes="x" * 80)
                for index in range(size)
            )
            await session.commit()

    async def seed_crates() -> None:
        async with sessionmaker() as session:
            session.add_all(Crate(code=f"CRT-{index}", weight_grams=index) for index in range(size))
            await session.commit()

    async def seed_items_core() -> None:
        # Core seed so the update benchmark measures updates, not seed creates.
        async with sessionmaker() as session:
            await session.execute(
                sa.insert(Item),
                [
                    {
                        "name": f"Tracked {index}",
                        "sku": f"TRK-{index}",
                        "quantity": 0,
                        "is_active": True,
                        "notes": "x" * 80,
                        "meta": {},
                        "secret_token": "",
                        "internal_note": "",
                    }
                    for index in range(size)
                ],
            )
            await session.commit()

    async def tracked_update() -> None:
        async with sessionmaker() as session:
            rows = list(await session.scalars(sa.select(Item)))
            for row in rows:
                row.quantity += 1
            await session.commit()

    async def plain_update() -> None:
        async with sessionmaker() as session:
            rows = list(await session.scalars(sa.select(Crate)))
            for row in rows:
                row.weight_grams += 1
            await session.commit()

    async def measure(probe: Any, seed: Any = None) -> float:
        best = float("inf")
        for _ in range(repeats):
            await clear()
            if seed is not None:
                await seed()
            started = time.perf_counter()
            await probe()
            best = min(best, time.perf_counter() - started)
        return best

    plain_create_s = await measure(seed_crates)
    tracked_create_s = await measure(seed_items)
    plain_update_s = await measure(plain_update, seed=seed_crates)
    tracked_update_s = await measure(tracked_update, seed=seed_items_core)

    async with sessionmaker() as session:
        changes = await session.scalars(sa.select(RowChange.changes))
        payload_sizes = [len(json.dumps(row, default=str)) for row in changes]
    assert len(payload_sizes) == size * repeats * 2  # creates + updates
    avg_payload = sum(payload_sizes) / len(payload_sizes)

    def line(name: str, plain: float, tracked: float) -> str:
        plain_us = plain / size * 1_000_000
        tracked_us = tracked / size * 1_000_000
        return (
            f"  {name:7s} plain {plain_us:7.1f} us/row  tracked {tracked_us:7.1f} us/row"
            f"  (+{tracked_us - plain_us:6.1f}, x{tracked / plain:.2f})"
        )

    print(
        f"\nNF2 benchmark (size={size}, best of {repeats}, in-memory SQLite):"
        f"\n{line('create', plain_create_s, tracked_create_s)}"
        f"\n{line('update', plain_update_s, tracked_update_s)}"
        f"\n  entries {len(payload_sizes)} avg payload {avg_payload:.0f} B"
    )
