"""``history_anonymize`` (Phase 3): payload erasure, trigger handling, CLI."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from obox_builder.conf import SETTINGS_MODULE_ENV
from obox_builder.contrib.history.anonymize import (
    anonymize_actor,
    anonymize_object,
    erase_payload,
)
from obox_builder.contrib.history.context import use
from obox_builder.contrib.history.models import RowChange
from obox_builder.core.management.cli import create_typer_app
from obox_builder.db.base import ModelBase
from obox_builder.db.engine import run_async
from obox_builder.db.session import session_scope
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from typer.testing import CliRunner

from tests.support.apps.warehouse.models import Item


def test_erase_payload_keeps_the_skeleton() -> None:
    payload = {
        "fields": {"name": ["Anvil", "Bolt"], "quantity": [1, 2]},
        "m2m": {"tags": {"add": ["3"], "remove": []}},
        "masked": {"email": ["a***@example.com", "a***@example.com"]},
        "redacted": ["secret_token"],
        "truncated": {"notes": {"len": 9000, "sha256": "abc"}},
        "unknown_old": ["meta"],
    }
    erased = erase_payload(payload)
    assert erased["fields"] == {}
    assert erased["m2m"] == {}
    assert erased["masked"] == {}
    assert erased["truncated"] == {}
    assert erased["redacted"] == ["email", "name", "quantity", "secret_token"]
    assert erased["unknown_old"] == ["meta"]


async def _seed_actor_entries(db: AsyncSession, actor_id: int, username: str) -> list[int]:
    ids: list[int] = []
    for index in range(3):
        item = Item(name=f"Anvil {index}", sku=f"SKU-{index}")
        async with use(
            source="cli",
            actor_id=actor_id,
            actor_label=username,
            ip="10.0.0.1",
            user_agent="pytest",
        ):
            db.add(item)
            await db.flush()
        ids.append(item.id)
    await db.commit()
    return ids


async def test_anonymize_actor_blanks_the_trail(db: AsyncSession) -> None:
    await _seed_actor_entries(db, 7, "dana")
    async with use(source="cli", actor_id=8, actor_label="erin"):
        db.add(Item(name="Other", sku="SKU-OTHER"))
        await db.flush()
    await db.commit()

    dry = await anonymize_actor(db, 7, dry_run=True)
    assert (dry.updated, dry.dry_run) == (3, True)
    assert await db.scalar(sa.select(sa.func.count()).where(RowChange.actor_label == "dana")) == 3

    result = await anonymize_actor(db, 7, batch_size=2)
    assert result.updated == 3
    assert result.batches == 2

    rows = list(await db.scalars(sa.select(RowChange).where(RowChange.op == "create")))
    dana_rows = [row for row in rows if row.object_pk != "3" or row.actor_label != "erin"]
    keep = [row for row in rows if row.actor_label == "erin"]
    assert keep
    assert keep[0].actor_id == 8
    for row in dana_rows:
        if row.actor_label in ("erin", "system"):
            continue
        assert row.actor_id is None
        assert row.actor_label == "anonymous"
        assert row.ip is None
        assert row.user_agent is None


async def test_anonymize_object_erases_values_only(db: AsyncSession) -> None:
    async with use(source="cli", actor_id=7, actor_label="dana"):
        item = Item(name="Anvil", sku="ANV-1", quantity=3)
        db.add(item)
        await db.flush()
    item_id = item.id
    item.quantity = 5
    await db.commit()
    async with session_scope() as other:
        row = await other.get(Item, item_id)
        assert row is not None
        row.name = "Bolt"
    db.expire_all()

    dry = await anonymize_object(
        db, model_label="warehouse.item", object_pk=str(item_id), dry_run=True
    )
    assert (dry.updated, dry.dry_run) == (3, True)

    result = await anonymize_object(db, model_label="warehouse.item", object_pk=str(item_id))
    assert result.updated == 3

    rows = list(
        await db.scalars(
            sa.select(RowChange)
            .where(RowChange.model_label == "warehouse.item", RowChange.object_pk == str(item_id))
            .order_by(RowChange.id)
        )
    )
    assert [row.op for row in rows] == ["create", "update", "update"]
    for row in rows:
        assert row.changes["fields"] == {}
        assert row.changes["masked"] == {}
        assert row.changes["m2m"] == {}
        assert row.object_repr == f"#{item_id}"
    assert rows[0].changes["redacted"] == ["is_active", "meta", "name", "notes", "quantity", "sku"]
    assert rows[0].actor_id == 7  # the who/when skeleton survives


async def test_anonymize_restores_the_sqlite_guard_trigger(db: AsyncSession) -> None:
    if db.get_bind().dialect.name == "postgresql":
        pytest.skip("SQLite guard-trigger check")
    await _seed_actor_entries(db, 7, "dana")
    await anonymize_actor(db, 7)

    with pytest.raises(IntegrityError):
        await db.execute(sa.update(RowChange).values(actor_label="tampered"))
        await db.commit()
    await db.rollback()


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
    async with session_scope() as db, use(source="cli", actor_id=42, actor_label="dana"):
        db.add(Item(name="Anvil", sku="ANV-1"))
        await db.flush()


def test_command_anonymizes_actor_and_object(env: None) -> None:
    run_async(_seed_cli())
    runner = CliRunner()

    dry = runner.invoke(create_typer_app(), ["history_anonymize", "--actor-id", "42", "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert "Would update 1 row(s) (actor_id=42)." in dry.output

    real = runner.invoke(create_typer_app(), ["history_anonymize", "--actor-id", "42"])
    assert real.exit_code == 0, real.output
    assert "Updated 1 row(s) (actor_id=42)." in real.output

    by_object = runner.invoke(
        create_typer_app(),
        ["history_anonymize", "--model", "warehouse.item", "--pk", "1"],
    )
    assert by_object.exit_code == 0, by_object.output
    assert "Updated 1 row(s) (warehouse.item#1)." in by_object.output


def test_command_validates_the_scope_flags(env: None) -> None:
    run_async(_seed_cli())
    runner = CliRunner()

    missing = runner.invoke(create_typer_app(), ["history_anonymize"])
    assert missing.exit_code == 1
    assert "Pass --actor-id or both --model and --pk." in missing.output

    both = runner.invoke(
        create_typer_app(),
        ["history_anonymize", "--actor-id", "1", "--model", "warehouse.item", "--pk", "1"],
    )
    assert both.exit_code == 1
    assert "cannot be combined" in both.output

    half = runner.invoke(create_typer_app(), ["history_anonymize", "--model", "warehouse.item"])
    assert half.exit_code == 1
    assert "must be passed together" in half.output

    bad_model = runner.invoke(
        create_typer_app(), ["history_anonymize", "--model", "NOPE", "--pk", "1"]
    )
    assert bad_model.exit_code == 1
    assert "Invalid model label" in bad_model.output

    bad_batch = runner.invoke(
        create_typer_app(), ["history_anonymize", "--actor-id", "1", "--batch-size", "0"]
    )
    assert bad_batch.exit_code == 1
    assert "--batch-size" in bad_batch.output
