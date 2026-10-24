"""Admin integration: history card, timeline page, permissions, bulk delete."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from obox_builder.contrib.admin import ModelAdmin
from obox_builder.contrib.history.models import RowChange
from obox_builder.db.session import session_scope

from tests.admin.conftest import (
    build_app,
    confirmed_delete,
    csrf_for,
    ensure_permissions,
    grant_permissions,
    login_session,
    make_user,
)
from tests.support.apps.warehouse.models import Crate, Item

LIST_URL = "/admin/warehouse/item"
CRATE_URL = "/admin/warehouse/crate"


def _register_admins() -> None:
    from obox_builder.contrib.admin.site import site

    if site.get_model_admin_for(Item) is None:
        site.register(
            Item,
            type(
                "ItemAdmin",
                (ModelAdmin,),
                {"list_display": ("id", "name", "quantity"), "ordering": ("-id",)},
            ),
        )
    if site.get_model_admin_for(Crate) is None:
        site.register(
            Crate,
            type(
                "CrateAdmin",
                (ModelAdmin,),
                {"list_display": ("id", "code"), "ordering": ("-id",)},
            ),
        )


@pytest.fixture
async def client(history_env: None) -> AsyncIterator[httpx.AsyncClient]:
    from obox_builder.db import dispose, get_engine
    from obox_builder.db.base import ModelBase

    _register_admins()
    async with get_engine().begin() as connection:
        await connection.run_sync(ModelBase.metadata.drop_all)
        await connection.run_sync(ModelBase.metadata.create_all)
    await ensure_permissions()
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http
    await dispose()


async def make_item(name: str = "Anvil", **values: Any) -> int:
    values.setdefault("sku", f"SKU-{name.upper()}")
    async with session_scope() as db:
        item = Item(name=name, **values)
        db.add(item)
        await db.flush()
        return item.id


async def make_crate(code: str = "CRT-1") -> int:
    async with session_scope() as db:
        crate = Crate(code=code)
        db.add(crate)
        await db.flush()
        return crate.id


def item_form(**overrides: str) -> dict[str, str]:
    data = {
        "name": "Anvil",
        "sku": "SKU-ANVIL",
        "quantity": "3",
        "price": "",
        "released_on": "",
        "checked_at": "",
        "is_active": "true",
        "notes": "",
        "meta": "{}",
        "internal_note": "",
        "shelf_id": "",
    }
    data.update(overrides)
    return data


# -- permissions -------------------------------------------------------------


async def test_history_timeline_requires_the_history_permission(
    client: httpx.AsyncClient,
) -> None:
    item_id = await make_item()
    await make_user("editor")
    await grant_permissions("editor", "warehouse.view_item", "warehouse.change_item")
    await login_session(client, "editor")

    form = await client.get(f"{LIST_URL}/{item_id}", headers={"accept": "text/html"})
    assert form.status_code == 200
    assert "Full history" not in form.text

    listing = await client.get(LIST_URL, headers={"accept": "text/html"})
    assert listing.status_code == 200
    assert "data-history-url" not in listing.text

    denied = await client.get(f"{LIST_URL}/{item_id}/history", headers={"accept": "text/html"})
    assert denied.status_code == 403


async def test_history_permission_alone_is_not_enough(client: httpx.AsyncClient) -> None:
    item_id = await make_item()
    await make_user("auditor")
    await grant_permissions("auditor", "warehouse.change_item", "history.view_row_change")
    await login_session(client, "auditor")

    denied = await client.get(f"{LIST_URL}/{item_id}/history", headers={"accept": "text/html"})
    assert denied.status_code == 403


async def test_reader_with_both_permissions_sees_the_timeline(
    client: httpx.AsyncClient,
) -> None:
    item_id = await make_item()
    await make_user("auditor")
    await grant_permissions(
        "auditor",
        "warehouse.view_item",
        "warehouse.change_item",
        "history.view_row_change",
    )
    await login_session(client, "auditor")

    form = await client.get(f"{LIST_URL}/{item_id}", headers={"accept": "text/html"})
    assert form.status_code == 200
    assert "Full history" in form.text

    listing = await client.get(LIST_URL, headers={"accept": "text/html"})
    assert listing.status_code == 200
    assert f'data-history-url="{LIST_URL}/{item_id}/history"' in listing.text

    timeline = await client.get(f"{LIST_URL}/{item_id}/history", headers={"accept": "text/html"})
    assert timeline.status_code == 200
    assert "create" in timeline.text
    assert "system" in timeline.text
    assert "Name" in timeline.text


# -- timeline page & card ----------------------------------------------------


async def test_edit_form_shows_history_card_and_link(client: httpx.AsyncClient) -> None:
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    item_id = await make_item()

    response = await client.get(f"{LIST_URL}/{item_id}", headers={"accept": "text/html"})
    assert response.status_code == 200
    assert "History" in response.text
    assert f'href="{LIST_URL}/{item_id}/history"' in response.text


async def test_timeline_pages_with_a_before_cursor(client: httpx.AsyncClient) -> None:
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    item_id = await make_item()
    async with session_scope() as db:
        for quantity in range(1, 26):
            item = await db.get(Item, item_id)
            assert item is not None
            item.quantity = quantity
            await db.flush()
    timeline_url = f"{LIST_URL}/{item_id}/history"

    first = await client.get(timeline_url, headers={"accept": "text/html"})
    assert first.status_code == 200
    assert "26 changes recorded" in first.text
    assert "Older" in first.text
    assert "Newest" not in first.text

    async with session_scope() as db:
        oldest_on_page = await db.scalar(
            sa.select(RowChange.id)
            .where(RowChange.model_label == "warehouse.item")
            .order_by(RowChange.id.desc())
            .offset(19)
        )
    assert oldest_on_page is not None
    older = await client.get(
        f"{timeline_url}?before={oldest_on_page}", headers={"accept": "text/html"}
    )
    assert older.status_code == 200
    assert "Older" not in older.text
    assert "Newest" in older.text


async def test_untracked_model_has_no_history_ui(client: httpx.AsyncClient) -> None:
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    crate_id = await make_crate()

    form = await client.get(f"{CRATE_URL}/{crate_id}", headers={"accept": "text/html"})
    assert form.status_code == 200
    assert "Full history" not in form.text

    listing = await client.get(CRATE_URL, headers={"accept": "text/html"})
    assert listing.status_code == 200
    assert "data-history-url" not in listing.text

    missing = await client.get(f"{CRATE_URL}/{crate_id}/history", headers={"accept": "text/html"})
    assert missing.status_code == 404


# -- capture from the admin --------------------------------------------------


async def test_admin_save_records_the_actor(client: httpx.AsyncClient) -> None:
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    token = await csrf_for("alice")
    item_id = await make_item()

    response = await client.post(
        f"{LIST_URL}/{item_id}",
        data={"csrf_token": token, "_action": "save", **item_form(quantity="9")},
        follow_redirects=False,
    )
    assert response.status_code == 303

    async with session_scope() as db:
        row = await db.scalar(
            sa.select(RowChange).where(RowChange.op == "update").order_by(RowChange.id.desc())
        )
        assert row is not None
        assert row.actor_label == "alice"
        assert row.actor_id is not None
        assert row.source == "admin_ui"
        assert row.changes["fields"] == {"quantity": [0, 9]}


async def test_bulk_delete_records_one_entry_per_row(client: httpx.AsyncClient) -> None:
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    token = await csrf_for("alice")
    first = await make_item("Anvil")
    second = await make_item("Bolt")

    response = await confirmed_delete(
        client,
        f"{LIST_URL}/actions",
        data={
            "csrf_token": token,
            "action": "delete_selected",
            "select_across": "0",
            "_selected_action": [str(first), str(second)],
        },
    )
    assert response.status_code == 303

    async with session_scope() as db:
        rows = list(
            await db.scalars(
                sa.select(RowChange)
                .where(RowChange.op == "delete", RowChange.actor_label == "alice")
                .order_by(RowChange.id)
            )
        )
    assert [row.object_pk for row in rows] == [str(first), str(second)]
    assert rows[0].object_repr == "Anvil"
    assert rows[1].object_repr == "Bolt"
    assert all(row.changes["fields"]["name"][1] is None for row in rows)


# -- track-all setting -------------------------------------------------------


async def test_track_all_admin_models_setting(history_env: None, override_settings: Any) -> None:
    from obox_builder.contrib.admin.apps import AdminConfig
    from obox_builder.contrib.admin.site import site
    from obox_builder.contrib.history import is_tracked

    _register_admins()
    assert site.get_model_admin_for(Crate) is not None
    assert not is_tracked(Crate)

    override_settings(HISTORY_TRACK_ALL_ADMIN_MODELS=True)
    AdminConfig("obox_builder.contrib.admin").ready()

    assert is_tracked(Crate)
    assert is_tracked(Item)
