"""``history_relabel``: model renames must not orphan the timeline."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from obox_builder.conf import SETTINGS_MODULE_ENV
from obox_builder.contrib.history.models import RowChange
from obox_builder.contrib.history.relabel import relabel
from obox_builder.contrib.history.service import MODEL_LABEL_RE
from obox_builder.core.management.cli import create_typer_app
from obox_builder.db.base import ModelBase
from obox_builder.db.engine import run_async
from obox_builder.db.session import session_scope
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from typer.testing import CliRunner

from tests.support.apps.warehouse.models import Item


def test_model_label_regex() -> None:
    assert MODEL_LABEL_RE.match("warehouse.item")
    assert not MODEL_LABEL_RE.match("Warehouse.Item")
    assert not MODEL_LABEL_RE.match("warehouse")
    assert not MODEL_LABEL_RE.match("ware house.item")


async def test_relabel_rewrites_only_the_label(db: AsyncSession) -> None:
    db.add_all(Item(name=f"Anvil {index}", sku=f"SKU-{index}") for index in range(3))
    await db.commit()

    dry = await relabel(db, from_label="warehouse.item", to_label="warehouse.widget", dry_run=True)
    assert dry.updated == 3
    assert (
        await db.scalar(sa.select(sa.func.count()).where(RowChange.model_label == "warehouse.item"))
        == 3
    )

    result = await relabel(
        db, from_label="warehouse.item", to_label="warehouse.widget", batch_size=2
    )
    assert result.updated == 3
    assert result.batches == 2

    rows = list(await db.scalars(sa.select(RowChange)))
    assert {row.model_label for row in rows} == {"warehouse.widget"}
    assert {row.op for row in rows} == {"create"}
    assert all(row.changes["fields"]["name"][1].startswith("Anvil") for row in rows)


async def test_relabel_validates_input(db: AsyncSession) -> None:
    with pytest.raises(ValueError, match="must differ"):
        await relabel(db, from_label="a.b", to_label="a.b")
    with pytest.raises(ValueError, match="batch_size"):
        await relabel(db, from_label="a.b", to_label="a.c", batch_size=0)


async def test_relabel_restores_the_sqlite_guard_trigger(db: AsyncSession) -> None:
    if db.get_bind().dialect.name == "postgresql":
        pytest.skip("SQLite guard-trigger check")
    db.add(Item(name="Anvil", sku="SKU-1"))
    await db.commit()

    await relabel(db, from_label="warehouse.item", to_label="warehouse.old_item")

    with pytest.raises(IntegrityError):
        await db.execute(sa.update(RowChange).values(model_label="tampered"))
        await db.commit()
    await db.rollback()


# -- CLI ---------------------------------------------------------------------


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(SETTINGS_MODULE_ENV, "tests.support.history_settings")
    monkeypatch.setenv("OBOX_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite3'}")


async def _seed() -> None:
    import obox_builder
    from obox_builder.db import get_engine

    obox_builder.setup()
    async with get_engine().begin() as connection:
        await connection.run_sync(ModelBase.metadata.create_all)
    async with session_scope() as db:
        db.add(Item(name="Anvil", sku="ANV-1"))


def test_command_relabels(env: None) -> None:
    run_async(_seed())
    runner = CliRunner()

    dry = runner.invoke(
        create_typer_app(),
        ["history_relabel", "--from", "warehouse.item", "--to", "warehouse.widget", "--dry-run"],
    )
    assert dry.exit_code == 0, dry.output
    assert "Would update 1 row(s): warehouse.item -> warehouse.widget." in dry.output

    real = runner.invoke(
        create_typer_app(),
        ["history_relabel", "--from", "warehouse.item", "--to", "warehouse.widget"],
    )
    assert real.exit_code == 0, real.output
    assert "Updated 1 row(s): warehouse.item -> warehouse.widget." in real.output


def test_command_rejects_bad_input(env: None) -> None:
    run_async(_seed())
    runner = CliRunner()

    bad_from = runner.invoke(
        create_typer_app(), ["history_relabel", "--from", "NOPE", "--to", "warehouse.widget"]
    )
    assert bad_from.exit_code == 1
    assert "Invalid model label" in bad_from.output

    same = runner.invoke(
        create_typer_app(),
        ["history_relabel", "--from", "warehouse.item", "--to", "warehouse.item"],
    )
    assert same.exit_code == 1
    assert "must differ" in same.output

    bad_batch = runner.invoke(
        create_typer_app(),
        [
            "history_relabel",
            "--from",
            "warehouse.item",
            "--to",
            "warehouse.widget",
            "--batch-size",
            "99999",
        ],
    )
    assert bad_batch.exit_code == 1
    assert "--batch-size" in bad_batch.output
