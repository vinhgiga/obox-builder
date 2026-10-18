"""Retention: ``history.purge`` batching, dry runs and retention resolution."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

import pytest
import sqlalchemy as sa
from obox_builder.contrib.history.immutability import install
from obox_builder.contrib.history.models import RowChange
from obox_builder.contrib.history.purge import effective_days, purge
from obox_builder.core.utils.time import utcnow
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.apps.warehouse.models import Item


async def _seed(db: AsyncSession, *, age_days: int, name: str = "Anvil") -> Item:
    item = Item(name=name, sku=f"SKU-{name}")
    db.add(item)
    await db.commit()
    await db.execute(
        sa.update(RowChange)
        .where(RowChange.object_pk == str(item.id))
        .values(occurred_at=utcnow() - timedelta(days=age_days))
    )
    await db.commit()
    return item


async def _count(db: AsyncSession) -> int:
    return int((await db.execute(sa.select(sa.func.count(RowChange.id)))).scalar_one())


async def test_purge_deletes_expired_and_keeps_fresh(db: AsyncSession) -> None:
    old = await _seed(db, age_days=40)
    fresh = await _seed(db, age_days=1, name="Newt")

    result = await purge(db, days=30)

    assert result.deleted == 1
    assert result.skipped == []
    rows = list(await db.scalars(sa.select(RowChange)))
    assert [row.object_pk for row in rows] == [str(fresh.id)]
    assert old.id is not None


async def test_purge_without_any_retention_skips_everything(db: AsyncSession) -> None:
    await _seed(db, age_days=4_000)

    result = await purge(db)

    assert result.deleted == 0
    assert result.skipped == ["warehouse.item"]
    assert await _count(db) == 1


async def test_purge_batches_respect_batch_size(db: AsyncSession) -> None:
    for index in range(5):
        await _seed(db, age_days=90, name=f"Item {index}")

    result = await purge(db, days=1, batch_size=2, pause=0)

    assert result.deleted == 5
    assert result.batches == 3
    assert await _count(db) == 0


async def test_purge_dry_run_reports_without_deleting(db: AsyncSession) -> None:
    await _seed(db, age_days=90)

    result = await purge(db, days=1, dry_run=True)

    assert result.deleted == 1
    assert result.dry_run is True
    assert await _count(db) == 1


async def test_purge_works_with_the_sqlite_trigger_installed(db: AsyncSession) -> None:
    await _seed(db, age_days=90)
    await install(db)
    await db.commit()

    result = await purge(db, days=1)

    assert result.deleted == 1


async def test_purge_rejects_bad_arguments(db: AsyncSession) -> None:
    with pytest.raises(ValueError, match="batch_size"):
        await purge(db, days=1, batch_size=0)
    with pytest.raises(ValueError, match="days"):
        await purge(db, days=-1)


def test_effective_days_prefers_explicit_then_model_then_global(
    history_env: None, override_settings: Callable[..., None]
) -> None:
    override_settings(HISTORY_RETENTION_DAYS=365)

    assert effective_days("warehouse.item", None, {}) == 365
    assert effective_days("warehouse.item", 7, {}) == 7
    assert effective_days("warehouse.item", None, {"warehouse.item": 30}) == 30
