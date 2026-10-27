"""Core capture semantics: CREATE/UPDATE/DELETE, payload v1, context, policies."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
import sqlalchemy as sa
from obox_builder.contrib.history.models import RowChange
from obox_builder.core.utils.time import utcnow
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.apps.warehouse.models import Crate, Item, Shelf


async def changes_for(db: AsyncSession, obj: object, op: str | None = None) -> list[RowChange]:
    from obox_builder.contrib.history.service import model_label

    stmt = (
        sa.select(RowChange)
        .where(
            RowChange.model_label == model_label(type(obj)),
            RowChange.object_pk == str(obj.id),  # type: ignore[attr-defined]
        )
        .order_by(RowChange.id)
    )
    rows = list(await db.scalars(stmt))
    if op is not None:
        rows = [row for row in rows if row.op == op]
    return rows


async def test_create_records_full_row(db: AsyncSession) -> None:
    item = Item(name="Anvil", sku="ANV-1", quantity=3)
    db.add(item)
    await db.commit()

    [row] = await changes_for(db, item, "create")
    assert row.object_repr == "Anvil"
    assert row.source == "system"
    assert row.actor_label == "system"
    assert row.actor_id is None
    assert row.schema_version == 1
    payload = row.changes
    assert set(payload) == {"fields", "m2m", "masked", "redacted", "truncated", "unknown_old"}
    assert payload["m2m"] == {}
    assert payload["masked"] == {}
    assert payload["redacted"] == []
    assert payload["truncated"] == {}
    assert payload["unknown_old"] == []
    assert payload["fields"]["name"] == [None, "Anvil"]
    assert payload["fields"]["quantity"] == [None, 3]
    assert payload["fields"]["is_active"] == [None, True]
    assert payload["fields"]["notes"] == [None, ""]
    assert payload["fields"]["meta"] == [None, {}]
    assert "secret_token" not in payload["fields"]
    assert "created_at" not in payload["fields"]
    assert "updated_at" not in payload["fields"]
    assert "id" not in payload["fields"]


async def test_update_records_only_changed_fields(db: AsyncSession) -> None:
    item = Item(name="Anvil", sku="ANV-1", quantity=3)
    db.add(item)
    await db.commit()

    item.quantity = 5
    await db.commit()

    [row] = await changes_for(db, item, "update")
    assert row.changes["fields"] == {"quantity": [3, 5]}
    assert row.object_repr == "Anvil"


async def test_update_no_effect_writes_nothing(db: AsyncSession) -> None:
    item = Item(name="Anvil", sku="ANV-1", quantity=3)
    db.add(item)
    await db.commit()

    item.quantity = 4
    item.quantity = 3
    await db.commit()

    assert await changes_for(db, item, "update") == []


async def test_delete_snapshots_loaded_values(db: AsyncSession) -> None:
    item = Item(name="Anvil", sku="ANV-1", quantity=3)
    db.add(item)
    await db.commit()

    await db.delete(item)
    await db.commit()

    [row] = await changes_for(db, item, "delete")
    assert row.changes["fields"]["name"] == ["Anvil", None]
    assert row.changes["fields"]["quantity"] == [3, None]
    assert row.object_repr == "Anvil"


async def test_secret_is_redacted_not_recorded(db: AsyncSession) -> None:
    item = Item(name="Anvil", sku="ANV-1", secret_token="s3cr3t")
    db.add(item)
    await db.commit()

    [row] = await changes_for(db, item, "create")
    assert row.changes["redacted"] == ["secret_token"]
    assert "secret_token" not in row.changes["fields"]

    item.secret_token = "r0tated"
    await db.commit()
    [row] = await changes_for(db, item, "update")
    assert row.changes["redacted"] == ["secret_token"]
    assert row.changes["fields"] == {}


async def test_history_false_fields_are_never_recorded(db: AsyncSession) -> None:
    item = Item(name="Anvil", sku="ANV-1", internal_note="do not log")
    db.add(item)
    await db.commit()
    [row] = await changes_for(db, item, "create")
    assert "internal_note" not in row.changes["fields"]


async def test_diff_repr_normalises_numbers_dates_and_links(db: AsyncSession) -> None:
    shelf = Shelf(code="A-1")
    db.add(shelf)
    await db.commit()

    item = Item(
        name="Anvil",
        sku="ANV-1",
        price=Decimal("12.50"),
        released_on=date(2026, 1, 2),
        checked_at=datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc),
        shelf_id=shelf.id,
    )
    db.add(item)
    await db.commit()
    [row] = await changes_for(db, item, "create")
    assert row.changes["fields"]["price"] == [None, "12.50"]
    assert row.changes["fields"]["released_on"] == [None, "2026-01-02"]
    assert row.changes["fields"]["checked_at"] == [None, "2026-01-02T03:04:00+00:00"]
    assert row.changes["fields"]["shelf_id"] == [None, shelf.id]


async def test_optionally_tracked_model_captures_nothing_until_tracked(
    db: AsyncSession,
) -> None:
    from obox_builder.contrib import history

    crate = Crate(code="C-1")
    db.add(crate)
    await db.commit()
    assert await changes_for(db, crate) == []

    history.track(Crate)
    crate.weight_grams = 100
    await db.commit()
    [row] = await changes_for(db, crate, "update")
    assert row.changes["fields"] == {"weight_grams": [0, 100]}


async def test_change_set_shared_and_monotonic_ids(db: AsyncSession) -> None:
    first = Item(name="A", sku="A-1")
    second = Item(name="B", sku="B-1")
    db.add_all([first, second])
    await db.commit()

    [row_a] = await changes_for(db, first, "create")
    [row_b] = await changes_for(db, second, "create")
    assert row_a.change_set_id == row_b.change_set_id
    assert row_a.id < row_b.id

    first.quantity = 1
    await db.commit()
    [update_row] = await changes_for(db, first, "update")
    assert update_row.change_set_id != row_a.change_set_id


async def test_payload_for_one_row_matches_golden_shape(db: AsyncSession) -> None:
    item = Item(name="Anvil", sku="ANV-1", quantity=1)
    db.add(item)
    await db.commit()
    item.quantity = 2
    item.name = "Anvil II"
    await db.commit()

    [row] = await changes_for(db, item, "update")
    assert row.changes == {
        "fields": {
            "name": [["Anvil"][0], "Anvil II"],
            "quantity": [1, 2],
        },
        "m2m": {},
        "masked": {},
        "redacted": [],
        "truncated": {},
        "unknown_old": [],
    }


async def test_context_from_session_info_is_used(db: AsyncSession) -> None:
    from obox_builder.contrib.history.context import HistoryContext

    db.info["obox.history.ctx"] = HistoryContext(
        actor_id=7,
        actor_label="vinh",
        source="cli",
        request_id="req-1",
        ip="10.0.0.1",
        user_agent="pytest/1.0",
        reason="backfill",
    )
    item = Item(name="Anvil", sku="ANV-1")
    db.add(item)
    await db.commit()

    [row] = await changes_for(db, item, "create")
    assert (row.actor_id, row.actor_label, row.source) == (7, "vinh", "cli")
    assert (row.request_id, row.ip, row.user_agent, row.reason) == (
        "req-1",
        "10.0.0.1",
        "pytest/1.0",
        "backfill",
    )


async def test_late_bound_context_resolver_reads_request_state(db: AsyncSession) -> None:
    from types import SimpleNamespace

    from obox_builder.contrib.history import context as history_context

    request = SimpleNamespace(
        state=SimpleNamespace(
            user=SimpleNamespace(id=3, username="admin"),
            auth_session=SimpleNamespace(user_agent="agent-x"),
            request_id="header value!",
        ),
        client=SimpleNamespace(host="192.0.2.10"),
        headers={"x-forwarded-for": "203.0.113.9, 10.0.0.2"},
    )
    history_context.bind(db, request)

    item = Item(name="Anvil", sku="ANV-1")
    db.add(item)
    await db.commit()

    [row] = await changes_for(db, item, "create")
    assert (row.actor_id, row.actor_label, row.source) == (3, "admin", "admin_ui")
    assert row.request_id == "headervalue"  # sanitized to [A-Za-z0-9._-]
    assert row.ip == "192.0.2.10"  # XFF ignored: peer not a trusted proxy
    assert row.user_agent == "agent-x"


async def test_orm_dml_bypasses_history_and_warns(
    db: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    item = Item(name="Anvil", sku="ANV-1", quantity=1)
    db.add(item)
    await db.commit()

    with caplog.at_level("WARNING", logger="obox.history"):
        await db.execute(sa.update(Item).values(quantity=9))
        await db.commit()

    assert any("bypasses row history" in record.message % record.args for record in caplog.records)
    assert await changes_for(db, item, "update") == []


async def test_orm_dml_can_raise_when_policy_demands(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from obox_builder.contrib.history.capture import BulkWriteNotTrackedError

    monkeypatch.setattr(
        "obox_builder.contrib.history.capture.settings.HISTORY_BULK_POLICY", "raise"
    )
    item = Item(name="Anvil", sku="ANV-1")
    db.add(item)
    await db.commit()

    with pytest.raises(BulkWriteNotTrackedError):
        await db.execute(sa.delete(Item).where(Item.id == item.id))


async def test_bulk_dml_on_untracked_model_is_silent(
    db: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    from tests.support.apps.shop.models import Product

    with caplog.at_level("WARNING", logger="obox.history"):
        await db.execute(sa.delete(Product))
        await db.commit()
    assert caplog.records == []


async def test_truncation_marks_both_sides(
    db: AsyncSession, override_settings: Callable[..., None]
) -> None:
    override_settings(HISTORY_MAX_VALUE_BYTES=64)
    long_note = "x" * 100
    item = Item(name="Anvil", sku="ANV-1", notes=long_note)
    db.add(item)
    await db.commit()
    item.notes = "y" * 100
    await db.commit()

    [row] = await changes_for(db, item, "update")
    truncated = row.changes["truncated"]
    assert set(truncated) == {"notes"}
    assert truncated["notes"]["old"]["len"] == len(f'"{long_note}"')
    assert len(truncated["notes"]["old"]["sha256"]) == 64
    preview_old, preview_new = row.changes["fields"]["notes"]
    assert preview_old.startswith('"xxx')
    assert preview_new.startswith('"yyy')


async def test_unknown_old_when_attribute_never_loaded(db: AsyncSession) -> None:
    item = Item(name="Anvil", sku="ANV-1", quantity=3)
    db.add(item)
    await db.commit()

    db.expire(item, ["name"])  # the previous name is no longer known
    item.name = "Renamed"
    item.sku = "ANV-2"
    await db.commit()

    [row] = await changes_for(db, item, "update")
    assert row.changes["unknown_old"] == ["name"]
    assert row.changes["fields"] == {"sku": ["ANV-1", "ANV-2"]}


async def test_change_set_id_is_uuid(db: AsyncSession) -> None:
    item = Item(name="Anvil", sku="ANV-1")
    db.add(item)
    await db.commit()
    [row] = await changes_for(db, item, "create")
    assert isinstance(row.change_set_id, uuid.UUID)


async def test_occurred_at_close_to_now(db: AsyncSession) -> None:
    from obox_builder.core.utils.time import ensure_aware

    item = Item(name="Anvil", sku="ANV-1")
    db.add(item)
    await db.commit()
    [row] = await changes_for(db, item, "create")
    assert abs((utcnow() - ensure_aware(row.occurred_at)).total_seconds()) < 5


async def test_pending_queue_does_not_leak_on_rollback(db: AsyncSession) -> None:
    item = Item(name="Anvil", sku="ANV-1")
    db.add(item)
    await db.flush()
    await db.rollback()

    assert "obox.history.pending" not in db.info
    assert "obox.history.change_set" not in db.info
