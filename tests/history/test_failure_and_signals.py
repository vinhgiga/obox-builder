"""Failure modes (closed/open) and observability signals."""

from __future__ import annotations

import logging
from typing import Any

import pytest
import sqlalchemy as sa
from obox_builder.contrib.history import capture, signals
from obox_builder.contrib.history.models import RowChange
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.apps.warehouse.models import Item


def _boom_build_changes(change: Any) -> Any:
    raise RuntimeError("payload failed")


def _boom_insert(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError("insert failed")


async def item_count(db: AsyncSession) -> int:
    return int(await db.scalar(sa.text("select count(*) from warehouse_item")) or 0)


async def change_count(db: AsyncSession) -> int:
    return int(await db.scalar(sa.select(sa.func.count(RowChange.id))) or 0)


async def test_closed_mode_rolls_back_the_data_change(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(capture, "build_changes", _boom_build_changes)
    db.add(Item(name="Anvil", sku="ANV-1"))
    with pytest.raises(RuntimeError, match="payload failed"):
        await db.commit()
    await db.rollback()

    assert await item_count(db) == 0
    assert await change_count(db) == 0


async def test_closed_mode_rolls_back_when_the_insert_fails(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(capture, "insert", _boom_insert)
    db.add(Item(name="Anvil", sku="ANV-1"))
    with pytest.raises(RuntimeError, match="insert failed"):
        await db.commit()
    await db.rollback()

    assert await item_count(db) == 0
    assert await change_count(db) == 0


async def test_open_mode_swallows_a_payload_failure_and_commits(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    override_settings: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    override_settings(HISTORY_FAILURE_MODE="open")
    monkeypatch.setattr(capture, "build_changes", _boom_build_changes)
    with caplog.at_level(logging.ERROR, logger="obox.history"):
        db.add(Item(name="Anvil", sku="ANV-1"))
        await db.commit()

    assert await item_count(db) == 1
    assert await change_count(db) == 0
    assert any("Failed to record row history" in record.message for record in caplog.records)


async def test_open_mode_uses_a_savepoint_when_the_insert_fails(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    override_settings: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    override_settings(HISTORY_FAILURE_MODE="open")
    monkeypatch.setattr(capture, "insert", _boom_insert)
    with caplog.at_level(logging.ERROR, logger="obox.history"):
        db.add(Item(name="Anvil", sku="ANV-1"))
        await db.commit()

    assert await item_count(db) == 1
    assert await change_count(db) == 0
    assert any("Failed to record row history" in record.message for record in caplog.records)


async def test_pre_record_receivers_can_prune_pending_changes(db: AsyncSession) -> None:
    seen: list[Any] = []

    def prune(sender: Any, **kwargs: Any) -> None:
        seen.extend(kwargs["changes"])
        kwargs["changes"].clear()

    signals.pre_record.connect(prune, dispatch_uid="test-prune")
    try:
        db.add(Item(name="Anvil", sku="ANV-1"))
        await db.commit()
    finally:
        signals.pre_record.disconnect(prune)

    assert [change.op for change in seen] == ["create"]
    assert await item_count(db) == 1
    assert await change_count(db) == 0


async def test_post_record_receives_the_inserted_rows(db: AsyncSession) -> None:
    captured: list[Any] = []

    def record(sender: Any, **kwargs: Any) -> None:
        captured.extend(kwargs["rows"])

    signals.post_record.connect(record, dispatch_uid="test-record")
    try:
        db.add(Item(name="Anvil", sku="ANV-1"))
        await db.commit()
    finally:
        signals.post_record.disconnect(record)

    assert [row["op"] for row in captured] == ["create"]
    assert captured[0]["object_repr"] == "Anvil"
    assert captured[0]["changes"]["fields"]["name"] == [None, "Anvil"]
