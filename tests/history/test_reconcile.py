"""``history_reconcile``: blind spots (raw SQL/Core DML) are detected and repaired."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from obox_builder.conf import SETTINGS_MODULE_ENV
from obox_builder.contrib.history.models import RowChange
from obox_builder.contrib.history.reconcile import REPAIR_LABEL, reconcile
from obox_builder.core.management.cli import create_typer_app
from obox_builder.db.base import ModelBase
from obox_builder.db.engine import run_async
from obox_builder.db.session import session_scope
from sqlalchemy.ext.asyncio import AsyncSession
from typer.testing import CliRunner

from tests.support.apps.warehouse.models import Contact, Item


async def _entry_count(db: AsyncSession, *, source: str | None = None) -> int:
    stmt = sa.select(sa.func.count(RowChange.id))
    if source is not None:
        stmt = stmt.where(RowChange.source == source)
    return int(await db.scalar(stmt) or 0)


async def test_clean_history_has_no_drift(db: AsyncSession) -> None:
    db.add(Item(name="Anvil", sku="ANV-1"))
    await db.commit()

    result = await reconcile(db)
    assert result.checked >= 1
    assert result.drifts == []


async def test_detects_and_fixes_a_blind_update(db: AsyncSession) -> None:
    db.add(Item(name="Anvil", sku="ANV-1", quantity=3))
    await db.commit()

    await db.execute(sa.update(Item).values(quantity=99, name="Bolt"))
    await db.commit()

    found = await reconcile(db)
    (drift,) = [d for d in found.drifts if d.kind == "stale"]
    assert drift.object_pk == "1"
    assert "quantity" in drift.detail
    assert "name" in drift.detail
    assert found.fixed == 0

    fixed = await reconcile(db, fix=True)
    assert fixed.fixed == 1
    repaired = await db.scalar(
        sa.select(RowChange).where(RowChange.source == "db_trigger").order_by(RowChange.id)
    )
    assert repaired is not None
    assert repaired.actor_label == REPAIR_LABEL
    assert repaired.op == "update"
    assert repaired.changes["fields"]["quantity"] == [3, 99]

    assert (await reconcile(db)).drifts == []


async def test_detects_and_fixes_a_blind_insert(db: AsyncSession) -> None:
    await db.execute(
        sa.insert(Item),
        [
            {
                "name": "Blind",
                "sku": "BLD-1",
                "quantity": 1,
                "is_active": True,
                "notes": "",
                "meta": {},
                "secret_token": "",
                "internal_note": "",
            }
        ],
    )
    await db.commit()

    found = await reconcile(db)
    (drift,) = [d for d in found.drifts if d.kind == "unexpected"]
    assert drift.detail == "no history entries"

    fixed = await reconcile(db, fix=True)
    assert fixed.fixed == 1
    repaired = await db.scalar(
        sa.select(RowChange).where(RowChange.source == "db_trigger").order_by(RowChange.id)
    )
    assert repaired is not None
    assert repaired.op == "create"
    assert repaired.changes["fields"]["sku"] == [None, "BLD-1"]

    assert (await reconcile(db)).drifts == []


async def test_detects_a_blind_delete(db: AsyncSession) -> None:
    db.add(Item(name="Anvil", sku="ANV-1", quantity=5))
    await db.commit()

    await db.execute(sa.delete(Item).where(Item.id == 1))
    await db.commit()

    found = await reconcile(db)
    (drift,) = [d for d in found.drifts if d.kind == "missing"]
    assert drift.detail == "last op create"

    fixed = await reconcile(db, fix=True)
    assert fixed.fixed == 1
    repaired = await db.scalar(
        sa.select(RowChange).where(RowChange.source == "db_trigger").order_by(RowChange.id)
    )
    assert repaired is not None
    assert repaired.op == "delete"
    assert repaired.changes["fields"]["quantity"] == [5, None]

    assert (await reconcile(db)).drifts == []


async def test_resurrected_row_is_repaired(db: AsyncSession) -> None:
    db.add(Item(name="Anvil", sku="ANV-1", quantity=2))
    await db.commit()
    entry_id = await db.scalar(sa.select(RowChange.id))
    row = await db.get(Item, 1)
    assert row is not None
    await db.delete(row)
    await db.commit()
    assert entry_id is not None

    await db.execute(
        sa.insert(Item),
        [
            {
                "id": 1,
                "name": "Anvil",
                "sku": "ANV-1",
                "quantity": 2,
                "is_active": True,
                "notes": "",
                "meta": {},
                "secret_token": "",
                "internal_note": "",
            }
        ],
    )
    await db.commit()

    found = await reconcile(db)
    assert [d.kind for d in found.drifts] == ["resurrected"]

    fixed = await reconcile(db, fix=True)
    assert fixed.fixed == 1
    repaired = await db.scalar(
        sa.select(RowChange).where(RowChange.source == "db_trigger").order_by(RowChange.id)
    )
    assert repaired is not None
    assert repaired.op == "create"
    assert repaired.changes["fields"]["name"] == [None, "Anvil"]

    assert (await reconcile(db)).drifts == []


async def test_masked_values_are_never_compared(db: AsyncSession) -> None:
    db.add(Contact(name="Dana", email="dana@example.com"))
    await db.commit()

    await db.execute(sa.update(Contact).values(email="other@example.com"))
    await db.commit()

    result = await reconcile(db)
    assert result.drifts == []


async def test_fix_is_skipped_for_unregistered_models(db: AsyncSession) -> None:
    db.add(
        RowChange(
            change_set_id=__import__("uuid").uuid4(),
            op="create",
            model_label="legacy.thing",
            object_pk="1",
            changes={},
        )
    )
    await db.commit()

    result = await reconcile(db, model="legacy.thing", fix=True)
    assert result.skipped_labels == ["legacy.thing"]
    assert await _entry_count(db, source="db_trigger") == 0


async def test_reconcile_validates_batch_size(db: AsyncSession) -> None:
    with pytest.raises(ValueError, match="batch_size"):
        await reconcile(db, batch_size=0)


# -- CLI ---------------------------------------------------------------------


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(SETTINGS_MODULE_ENV, "tests.support.history_settings")
    monkeypatch.setenv("OBOX_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite3'}")


async def _seed_cli() -> None:
    import obox_builder
    from obox_builder.db import get_engine

    obox_builder.setup()
    async with get_engine().begin() as connection:
        await connection.run_sync(ModelBase.metadata.create_all)
    async with session_scope() as db:
        db.add(Item(name="Anvil", sku="ANV-1", quantity=3))


def test_command_reports_and_fixes(env: None) -> None:
    run_async(_seed_cli())

    async def blind_update() -> None:
        async with session_scope() as db:
            await db.execute(sa.update(Item).values(quantity=42))

    run_async(blind_update())
    runner = CliRunner()

    dry = runner.invoke(create_typer_app(), ["history_reconcile", "-v", "2"])
    assert dry.exit_code == 0, dry.output
    assert "warehouse.item#1: stale (fields: quantity)" in dry.output
    assert "Checked 1 row(s): 1 drift(s); Found 0." in dry.output

    fix = runner.invoke(create_typer_app(), ["history_reconcile", "-v", "2", "--fix"])
    assert fix.exit_code == 0, fix.output
    assert "Checked 1 row(s): 1 drift(s); Repaired 1." in fix.output

    again = runner.invoke(create_typer_app(), ["history_reconcile", "-v", "2"])
    assert again.exit_code == 0, again.output
    assert "Checked 1 row(s): 0 drift(s); Found 0." in again.output


def test_command_rejects_bad_input(env: None) -> None:
    run_async(_seed_cli())
    runner = CliRunner()

    bad_model = runner.invoke(create_typer_app(), ["history_reconcile", "--model", "NOPE"])
    assert bad_model.exit_code == 1
    assert "Invalid model label" in bad_model.output

    bad_batch = runner.invoke(create_typer_app(), ["history_reconcile", "--batch-size", "0"])
    assert bad_batch.exit_code == 1
    assert "--batch-size" in bad_batch.output
