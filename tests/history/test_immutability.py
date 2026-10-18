"""Append-only trigger: UPDATE blocked; purge works through maintenance mode."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from obox_builder.contrib.history.immutability import install, install_trigger_receiver
from obox_builder.contrib.history.models import RowChange
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.apps.warehouse.models import Item


def _dialect(db: AsyncSession) -> str:
    return db.get_bind().dialect.name


async def _seed(db: AsyncSession, sku: str = "ANV-1") -> None:
    item = Item(name="Anvil", sku=sku)
    db.add(item)
    await db.commit()


async def _count(db: AsyncSession) -> int:
    return int((await db.execute(sa.select(sa.func.count(RowChange.id)))).scalar_one())


async def test_sqlite_trigger_blocks_update_but_allows_delete(db: AsyncSession) -> None:
    if _dialect(db) == "postgresql":
        pytest.skip("covered by the PostgreSQL test")
    assert await install(db) is True
    await db.commit()
    await _seed(db)

    with pytest.raises(IntegrityError, match="append-only"):
        await db.execute(sa.update(RowChange).values(object_repr="tampered"))
        await db.commit()
    await db.rollback()

    await db.execute(sa.delete(RowChange))
    await db.commit()
    assert await _count(db) == 0


async def test_postgres_trigger_blocks_update_delete_and_truncate(db: AsyncSession) -> None:
    if _dialect(db) != "postgresql":
        pytest.skip("PostgreSQL-only")
    assert await install(db) is True
    await db.commit()
    await _seed(db)

    for statement in (
        sa.update(RowChange).values(object_repr="tampered"),
        sa.delete(RowChange),
        sa.text("TRUNCATE history_row_change"),
    ):
        with pytest.raises(ProgrammingError, match="append-only"):
            await db.execute(statement)
            await db.commit()
        await db.rollback()


async def test_postgres_maintenance_mode_allows_purge(db: AsyncSession) -> None:
    if _dialect(db) != "postgresql":
        pytest.skip("PostgreSQL-only")
    await install(db)
    await db.commit()
    await _seed(db)

    await db.execute(sa.text("SET LOCAL obox.history_maintenance = 'on'"))
    await db.execute(sa.delete(RowChange))
    await db.commit()
    assert await _count(db) == 0

    # A new transaction is protected again (SET LOCAL does not leak).
    await _seed(db, sku="ANV-2")
    with pytest.raises(ProgrammingError, match="append-only"):
        await db.execute(sa.delete(RowChange))
        await db.commit()
    await db.rollback()


async def test_install_is_idempotent(db: AsyncSession) -> None:
    await install(db)
    await install(db)
    await db.commit()
    await _seed(db)
    assert await _count(db) == 1


async def test_post_migrate_receiver_installs_the_trigger(
    db: AsyncSession, history_env: None
) -> None:
    if _dialect(db) == "postgresql":
        pytest.skip("PostgreSQL trigger covered above")
    await install_trigger_receiver(sender="migrate")
    await _seed(db)

    with pytest.raises(IntegrityError, match="append-only"):
        await db.execute(sa.update(RowChange).values(object_repr="tampered"))
        await db.commit()
    await db.rollback()
