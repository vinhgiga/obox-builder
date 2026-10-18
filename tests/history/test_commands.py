"""CLI wiring for ``history_purge`` and ``history_export``."""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from obox_builder.conf import SETTINGS_MODULE_ENV
from obox_builder.contrib.history.models import RowChange
from obox_builder.core.management.cli import create_typer_app
from obox_builder.core.utils.time import utcnow
from obox_builder.db.base import ModelBase
from obox_builder.db.engine import run_async
from obox_builder.db.session import session_scope
from typer.testing import CliRunner

from tests.support.apps.warehouse.models import Item


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A file-backed SQLite DB so state survives across engine lifecycles."""
    monkeypatch.setenv(SETTINGS_MODULE_ENV, "tests.support.history_settings")
    monkeypatch.setenv("OBOX_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite3'}")


async def seed(*, age_days: int | None = None) -> None:
    import obox_builder
    from obox_builder.db import get_engine

    obox_builder.setup()
    async with get_engine().begin() as connection:
        await connection.run_sync(ModelBase.metadata.create_all)
    async with session_scope() as db:
        db.add(Item(name="Anvil", sku="ANV-1"))
    if age_days is not None:
        async with session_scope() as db:
            await db.execute(
                sa.update(RowChange).values(occurred_at=utcnow() - timedelta(days=age_days))
            )


async def entry_count() -> int:
    async with session_scope() as db:
        return int((await db.execute(sa.select(sa.func.count(RowChange.id)))).scalar_one())


def test_history_purge_dry_run_then_delete(env: None) -> None:
    run_async(seed(age_days=90))
    runner = CliRunner()

    dry = runner.invoke(
        create_typer_app(), ["history_purge", "-v", "2", "--days", "30", "--dry-run"]
    )
    assert dry.exit_code == 0, dry.output
    assert "Would delete 1 row(s)." in dry.output
    assert run_async(entry_count()) == 1

    real = runner.invoke(create_typer_app(), ["history_purge", "-v", "2", "--days", "30"])
    assert real.exit_code == 0, real.output
    assert "Deleted 1 row(s) in 1 batch(es)." in real.output
    assert "warehouse.item: 1" in real.output
    assert run_async(entry_count()) == 0


def test_history_purge_without_retention_skips(env: None) -> None:
    run_async(seed(age_days=4_000))

    result = CliRunner().invoke(create_typer_app(), ["history_purge", "-v", "2"])

    assert result.exit_code == 0, result.output
    assert "Deleted 0 row(s)" in result.output
    assert "No retention configured (skipped): warehouse.item" in result.output
    assert run_async(entry_count()) == 1


def test_history_purge_rejects_bad_input(env: None) -> None:
    runner = CliRunner()

    bad_model = runner.invoke(create_typer_app(), ["history_purge", "--model", "Nope"])
    assert bad_model.exit_code == 1
    assert "Invalid model label" in bad_model.output

    bad_batch = runner.invoke(
        create_typer_app(), ["history_purge", "--days", "1", "--batch-size", "99999"]
    )
    assert bad_batch.exit_code == 1
    assert "--batch-size" in bad_batch.output


def test_history_export_ndjson_to_file(env: None, tmp_path: Path) -> None:
    run_async(seed())
    target = tmp_path / "audit.ndjson"
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    audit = logging.getLogger("obox.history.audit")
    audit.addHandler(handler)
    try:
        result = CliRunner().invoke(
            create_typer_app(), ["history_export", "--output", str(target), "--format", "ndjson"]
        )
    finally:
        audit.removeHandler(handler)

    assert result.exit_code == 0, result.output
    payload = json.loads(target.read_text().strip())
    assert payload["model_label"] == "warehouse.item"
    assert payload["op"] == "create"
    assert any("history_export: exported 1 row(s)" in record.getMessage() for record in records)


def test_history_export_csv_to_stdout(env: None) -> None:
    run_async(seed())

    result = CliRunner().invoke(create_typer_app(), ["history_export", "--format", "csv"])

    assert result.exit_code == 0, result.output
    header, first = result.stdout.splitlines()[:2]
    assert header.startswith("id,occurred_at,model_label,object_pk")
    assert ",create," in first


def test_history_export_rejects_bad_input(env: None, tmp_path: Path) -> None:
    runner = CliRunner()

    bad_format = runner.invoke(create_typer_app(), ["history_export", "--format", "xml"])
    assert bad_format.exit_code == 1
    assert "--format must be" in bad_format.output

    bad_date = runner.invoke(create_typer_app(), ["history_export", "--since", "yesterday"])
    assert bad_date.exit_code == 1
    assert "Invalid --since date" in bad_date.output
