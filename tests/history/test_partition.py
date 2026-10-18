"""``history_partition``: monthly RANGE partitions + retention by DROP (PG only).

The conversion recipe runs for real on PostgreSQL in CI (composite PK, attach,
DEFAULT partition, re-installed append-only trigger); on SQLite the CLI's
offline ``--sql`` output and the clean error paths are exercised.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from obox_builder.conf import SETTINGS_MODULE_ENV
from obox_builder.contrib.history.models import RowChange
from obox_builder.contrib.history.partition import (
    DEFAULT_PARTITION,
    PartitionError,
    add_months,
    conversion_sql,
    drop_before,
    ensure,
    month_start,
    partition_infos,
    partition_name,
)
from obox_builder.core.management.cli import create_typer_app
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from typer.testing import CliRunner


def _dialect(db: AsyncSession) -> str:
    return db.get_bind().dialect.name


def _entry(pk: str, *, occurred_at: datetime | None = None) -> RowChange:
    entry = RowChange(
        change_set_id=uuid.uuid4(),
        op="create",
        model_label="x.y",
        object_pk=pk,
        changes={},
    )
    if occurred_at is not None:
        entry.occurred_at = occurred_at
    return entry


async def _apply(db: AsyncSession, statements: list[str]) -> None:
    for statement in statements:
        if statement in ("BEGIN;", "COMMIT;"):
            continue
        await db.execute(sa.text(statement))
    await db.commit()


# -- SQLite / offline ---------------------------------------------------------


async def test_service_requires_postgresql(db: AsyncSession) -> None:
    if _dialect(db) == "postgresql":
        pytest.skip("SQLite error-path check")
    with pytest.raises(PartitionError, match="requires PostgreSQL"):
        await partition_infos(db)
    with pytest.raises(PartitionError, match="requires PostgreSQL"):
        await ensure(db)


def test_sql_prints_conversion_recipe() -> None:
    statements = conversion_sql(datetime(2026, 12, 1, tzinfo=UTC))
    script = "\n".join(statements)
    assert "PARTITION BY RANGE (occurred_at)" in script
    assert "ATTACH PARTITION history_row_change_legacy" in script
    assert "TIMESTAMPTZ '2026-12-01 00:00:00+00:00'" in script
    assert f"CREATE TABLE IF NOT EXISTS {DEFAULT_PARTITION}" in script
    assert "history_row_change_immutable" in script


def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(SETTINGS_MODULE_ENV, "tests.support.history_settings")
    monkeypatch.setenv("OBOX_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite3'}")


def test_cli_sql_and_flag_validation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _env(monkeypatch, tmp_path)
    runner = CliRunner()

    printed = runner.invoke(create_typer_app(), ["history_partition", "--sql"])
    assert printed.exit_code == 0, printed.output
    assert "PARTITION BY RANGE (occurred_at)" in printed.output

    bad_cutoff = runner.invoke(
        create_typer_app(), ["history_partition", "--sql", "--cutoff", "nope"]
    )
    assert bad_cutoff.exit_code == 1
    assert "Invalid month" in bad_cutoff.output

    both = runner.invoke(create_typer_app(), ["history_partition", "--ensure", "--sql"])
    assert both.exit_code == 1
    assert "exactly one" in both.output

    neither = runner.invoke(create_typer_app(), ["history_partition"])
    assert neither.exit_code == 1
    assert "exactly one" in neither.output

    dry_sql = runner.invoke(create_typer_app(), ["history_partition", "--sql", "--dry-run"])
    assert dry_sql.exit_code == 1
    assert "--dry-run has no effect" in dry_sql.output


def test_cli_requires_postgresql(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _env(monkeypatch, tmp_path)
    result = CliRunner().invoke(create_typer_app(), ["history_partition", "--ensure"])
    assert result.exit_code == 1
    assert "requires PostgreSQL" in result.output


# -- PostgreSQL ---------------------------------------------------------------


async def test_conversion_then_ensure_and_retention(db: AsyncSession) -> None:
    if _dialect(db) != "postgresql":
        pytest.skip("PostgreSQL-only")

    now = datetime.now(UTC)
    cutoff = month_start(now)
    old = cutoff - timedelta(days=40)

    try:
        await _conversion_scenario(db, now, cutoff, old)
    finally:
        await db.rollback()
        # The shared sequence is OWNED BY NONE and the legacy partition has a
        # non-metadata name, so drop_all alone would leak both into later tests.
        await db.execute(sa.text("DROP TABLE IF EXISTS history_row_change_legacy"))
        await db.execute(sa.text("DROP SEQUENCE IF EXISTS history_row_change_id_seq CASCADE"))
        await db.execute(sa.text("DROP FUNCTION IF EXISTS history_forbid_mutation() CASCADE"))
        await db.commit()


async def _conversion_scenario(
    db: AsyncSession, now: datetime, cutoff: datetime, old: datetime
) -> None:
    db.add_all([_entry("1", occurred_at=old), _entry("1b", occurred_at=old)])
    await db.commit()

    with pytest.raises(PartitionError, match="not partitioned yet"):
        await partition_infos(db)

    await _apply(db, conversion_sql(cutoff, concurrent=False))
    names = {info.name for info in await partition_infos(db)}
    assert names == {"history_row_change_legacy", DEFAULT_PARTITION}

    appended = await ensure(db, months=2, now=now)
    assert appended.created == [partition_name(now), partition_name(add_months(cutoff, 1))]

    db.add_all(
        [
            _entry("2"),
            _entry("3", occurred_at=add_months(cutoff, 6)),
            _entry("4", occurred_at=old),
        ]
    )
    await db.commit()

    rows = await db.execute(
        sa.text(
            "SELECT object_pk, tableoid::regclass::text FROM history_row_change "
            "WHERE object_pk IN ('2', '3', '4')"
        )
    )
    homes = {str(pk): str(home) for pk, home in rows.all()}
    assert homes["2"] == partition_name(now)
    assert homes["3"] == DEFAULT_PARTITION
    assert homes["4"] == "history_row_change_legacy"

    with pytest.raises(DBAPIError, match="append-only"):
        await db.execute(
            sa.update(RowChange).where(RowChange.object_pk == "2").values(reason="nope")
        )
    await db.rollback()

    result = await drop_before(db, month=cutoff.strftime("%Y-%m"))
    assert result.dropped == ["history_row_change_legacy"]
    assert result.rows_dropped == 3

    remaining = set(await db.scalars(sa.select(RowChange.object_pk)))
    assert remaining == {"2", "3"}

    revived = _entry("5", occurred_at=old)
    db.add(revived)
    await db.commit()
    assert revived.id is not None  # the shared sequence survived the partition drop

    dry = await drop_before(db, month=add_months(cutoff, 2).strftime("%Y-%m"), dry_run=True)
    assert dry.dropped == [partition_name(now), partition_name(add_months(cutoff, 1))]
    assert dry.rows_dropped == 1
    assert partition_name(now) in {info.name for info in await partition_infos(db)}

    dry_ensure = await ensure(db, months=3, now=now, dry_run=True)
    assert dry_ensure.created == [partition_name(add_months(cutoff, 2))]
    assert dry_ensure.kept == [
        partition_name(now),
        partition_name(add_months(cutoff, 1)),
        DEFAULT_PARTITION,
    ]
