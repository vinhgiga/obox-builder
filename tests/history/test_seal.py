"""Tamper evidence: ``history_seal`` / ``history_verify`` and the hash builders."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from obox_builder.conf import SETTINGS_MODULE_ENV
from obox_builder.contrib.history.models import RowChange, SealBlock
from obox_builder.contrib.history.seal import (
    block_hash,
    entry_hash,
    merkle_root,
    seal,
    verify,
)
from obox_builder.core.management.cli import create_typer_app
from obox_builder.core.utils.time import utcnow
from obox_builder.db.base import ModelBase
from obox_builder.db.engine import run_async
from obox_builder.db.session import session_scope
from sqlalchemy.ext.asyncio import AsyncSession
from typer.testing import CliRunner

from tests.support.apps.warehouse.models import Item


def test_merkle_root_and_block_hash_are_deterministic() -> None:
    leaves = [f"{index:064x}" for index in range(5)]
    root = merkle_root(leaves)
    assert root == merkle_root(leaves)
    assert len(root) == 64
    assert merkle_root([leaves[0]]) == leaves[0]
    assert merkle_root([*leaves, leaves[-1]]) == merkle_root(leaves)

    digest = block_hash(prev_block_hash=None, first_id=1, last_id=5, count=5, root=root)
    assert digest == block_hash(prev_block_hash=None, first_id=1, last_id=5, count=5, root=root)
    assert digest != block_hash(prev_block_hash="a" * 64, first_id=1, last_id=5, count=5, root=root)


async def _seed(db: AsyncSession, count: int = 5) -> None:
    db.add_all(Item(name=f"Anvil {index}", sku=f"SKU-{index}") for index in range(count))
    await db.commit()


async def test_seal_builds_chained_blocks(db: AsyncSession) -> None:
    await _seed(db, count=5)

    result = await seal(db, lag_seconds=0, batch_size=2)
    assert (result.sealed, result.blocks) == (5, 3)

    blocks = list(await db.scalars(sa.select(SealBlock).order_by(SealBlock.block_id)))
    assert [(block.first_id, block.last_id, block.count) for block in blocks] == [
        (1, 2, 2),
        (3, 4, 2),
        (5, 5, 1),
    ]
    assert blocks[0].prev_block_hash is None
    assert blocks[1].prev_block_hash == blocks[0].block_hash
    assert blocks[2].prev_block_hash == blocks[1].block_hash


async def test_seal_is_incremental_and_respects_the_lag(db: AsyncSession) -> None:
    await _seed(db, count=1)
    assert (await seal(db, lag_seconds=0)).blocks == 1
    assert (await seal(db, lag_seconds=0)).blocks == 0

    db.add(Item(name="Bolt", sku="SKU-X"))
    await db.commit()
    assert (await seal(db, lag_seconds=3600)).sealed == 0

    await db.execute(
        sa.update(RowChange)
        .where(RowChange.id > 1)
        .values(occurred_at=utcnow() - timedelta(hours=2))
    )
    await db.commit()
    assert (await seal(db, lag_seconds=60)).sealed == 1


async def test_verify_accepts_untouched_seals(db: AsyncSession) -> None:
    await _seed(db, count=3)
    await seal(db, lag_seconds=0, batch_size=2)

    result = await verify(db)
    assert result.ok
    assert (result.blocks, result.sealed_entries) == (2, 3)


async def test_verify_detects_an_edited_payload(db: AsyncSession) -> None:
    await _seed(db, count=2)
    await seal(db, lag_seconds=0, batch_size=10)
    entry = await db.scalar(sa.select(RowChange).order_by(RowChange.id))
    assert entry is not None

    await db.execute(
        sa.update(RowChange).where(RowChange.id == entry.id).values(object_repr="tampered")
    )
    await db.commit()

    result = await verify(db)
    assert not result.ok
    assert result.mismatched_blocks
    assert result.missing_count == 0


async def test_verify_detects_deleted_entries(db: AsyncSession) -> None:
    await _seed(db, count=3)
    await seal(db, lag_seconds=0, batch_size=10)
    entry = await db.scalar(sa.select(RowChange).order_by(RowChange.id))
    assert entry is not None

    await db.execute(sa.delete(RowChange).where(RowChange.id == entry.id))
    await db.commit()

    result = await verify(db)
    assert result.missing_ids == [entry.id]
    assert result.mismatched_blocks


async def test_verify_detects_a_rewritten_block(db: AsyncSession) -> None:
    await _seed(db, count=4)
    await seal(db, lag_seconds=0, batch_size=2)
    blocks = list(await db.scalars(sa.select(SealBlock).order_by(SealBlock.block_id)))

    await db.execute(
        sa.update(SealBlock)
        .where(SealBlock.block_id == blocks[0].block_id)
        .values(merkle_root="0" * 64)
    )
    await db.commit()

    result = await verify(db)
    assert blocks[0].block_id in result.broken_chain
    assert blocks[1].block_id not in result.broken_chain


async def test_entry_hash_covers_all_columns(db: AsyncSession) -> None:
    await _seed(db, count=1)
    entry = await db.scalar(sa.select(RowChange))
    assert entry is not None
    original = entry_hash(entry)
    original_repr = entry.object_repr

    entry.object_repr = "changed"
    assert entry_hash(entry) != original
    entry.object_repr = original_repr
    assert entry_hash(entry) == original


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
        db.add(Item(name="Anvil", sku="ANV-1"))


def test_commands_seal_and_verify(env: None) -> None:
    run_async(_seed_cli())
    runner = CliRunner()

    dry = runner.invoke(create_typer_app(), ["history_seal", "-v", "2", "--lag", "0", "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert "Would seal 1 entry(ies) in 1 block(s)." in dry.output
    assert "Range: [1..1]." in dry.output

    real = runner.invoke(create_typer_app(), ["history_seal", "-v", "2", "--lag", "0"])
    assert real.exit_code == 0, real.output
    assert "Sealed 1 entry(ies) in 1 block(s)." in real.output

    checked = runner.invoke(create_typer_app(), ["history_verify", "-v", "2"])
    assert checked.exit_code == 0, checked.output
    assert "OK: all seals verified." in checked.output


def test_verify_command_fails_on_tampering(env: None) -> None:
    run_async(_seed_cli())
    runner = CliRunner()
    runner.invoke(create_typer_app(), ["history_seal", "--lag", "0"])

    async def tamper() -> None:
        async with session_scope() as db:
            entry = await db.scalar(sa.select(RowChange))
            assert entry is not None
            entry.actor_label = "tampered"

    run_async(tamper())

    checked = runner.invoke(create_typer_app(), ["history_verify", "-v", "2"])
    assert checked.exit_code == 1
    assert "Mismatched blocks: 1" in checked.output


def test_seal_rejects_bad_input(env: None) -> None:
    run_async(_seed_cli())
    runner = CliRunner()

    bad_batch = runner.invoke(create_typer_app(), ["history_seal", "--batch-size", "0"])
    assert bad_batch.exit_code == 1
    assert "--batch-size" in bad_batch.output

    bad_lag = runner.invoke(create_typer_app(), ["history_seal", "--lag", "-1"])
    assert bad_lag.exit_code == 1
    assert "--lag" in bad_lag.output
