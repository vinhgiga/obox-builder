"""Global audit feed: permission-aware scope, filters, keyset paging, change sets."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import sqlalchemy as sa
from obox_builder.contrib.admin import ModelAdmin
from obox_builder.contrib.history.models import RowChange
from obox_builder.db.session import session_scope

from tests.admin.conftest import (
    build_app,
    csrf_for,
    ensure_permissions,
    grant_permissions,
    login_session,
    make_user,
)
from tests.history.test_admin_timeline import _register_admins, item_form, make_item
from tests.support.apps.warehouse.models import Item, Shelf

FEED_URL = "/admin/history"
LIST_URL = "/admin/warehouse/item"


def _register_shelf_admin() -> None:
    from obox_builder.contrib.admin.site import site

    if site.get_model_admin_for(Shelf) is None:
        site.register(
            Shelf,
            type(
                "ShelfAdmin",
                (ModelAdmin,),
                {"list_display": ("id", "code"), "ordering": ("-id",)},
            ),
        )


@pytest.fixture
async def client(history_env: None) -> AsyncIterator[httpx.AsyncClient]:
    from obox_builder.db import dispose, get_engine
    from obox_builder.db.base import ModelBase

    _register_admins()
    _register_shelf_admin()
    async with get_engine().begin() as connection:
        await connection.run_sync(ModelBase.metadata.drop_all)
        await connection.run_sync(ModelBase.metadata.create_all)
    await ensure_permissions()
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http
    await dispose()


async def make_shelf(code: str = "A-1") -> int:
    async with session_scope() as db:
        shelf = Shelf(code=code)
        db.add(shelf)
        await db.flush()
        return shelf.id


async def latest_change_set() -> str:
    async with session_scope() as db:
        row = await db.scalar(sa.select(RowChange).order_by(RowChange.id.desc()).limit(1))
        assert row is not None
        return str(row.change_set_id)


# -- permissions -------------------------------------------------------------


async def test_feed_requires_the_history_permission(client: httpx.AsyncClient) -> None:
    await make_item()
    await make_user("editor")
    await grant_permissions("editor", "warehouse.view_item")
    await login_session(client, "editor")

    denied = await client.get(FEED_URL, headers={"accept": "text/html"})
    assert denied.status_code == 403


async def test_feed_only_lists_models_the_viewer_can_view(client: httpx.AsyncClient) -> None:
    await make_item("Anvil")
    await make_shelf("A-1")
    await make_user("auditor")
    await grant_permissions("auditor", "warehouse.view_item", "history.view_row_change")
    await login_session(client, "auditor")

    feed = await client.get(FEED_URL, headers={"accept": "text/html"})
    assert feed.status_code == 200
    assert "Anvil" in feed.text
    assert "A-1" not in feed.text
    assert '<option value="warehouse.shelf"' not in feed.text

    await make_user("root", is_superuser=True)
    await login_session(client, "root")
    root_feed = await client.get(FEED_URL, headers={"accept": "text/html"})
    assert "Anvil" in root_feed.text
    assert "A-1" in root_feed.text


async def test_nav_shows_the_audit_log_for_privileged_users(client: httpx.AsyncClient) -> None:
    await make_user("auditor")
    await grant_permissions("auditor", "history.view_row_change")
    await login_session(client, "auditor")
    dashboard = await client.get("/admin/", headers={"accept": "text/html"})
    assert 'href="/admin/history"' in dashboard.text

    await make_user("editor")
    await grant_permissions("editor", "warehouse.view_item")
    await login_session(client, "editor")
    dashboard = await client.get("/admin/", headers={"accept": "text/html"})
    assert 'href="/admin/history"' not in dashboard.text


# -- filters -----------------------------------------------------------------


async def test_feed_filters_by_actor_source_op_and_model(client: httpx.AsyncClient) -> None:
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    token = await csrf_for("alice")
    item_id = await make_item("Anvil")
    await make_shelf("A-1")
    await client.post(
        f"{LIST_URL}/{item_id}",
        data={"csrf_token": token, "_action": "save", **item_form(quantity="9")},
        follow_redirects=False,
    )

    by_actor = await client.get(f"{FEED_URL}?actor=alice", headers={"accept": "text/html"})
    assert "Anvil" in by_actor.text
    assert "A-1" not in by_actor.text

    by_source = await client.get(f"{FEED_URL}?source=cli", headers={"accept": "text/html"})
    assert "No changes match these filters." in by_source.text

    by_op = await client.get(f"{FEED_URL}?op=delete", headers={"accept": "text/html"})
    assert "No changes match these filters." in by_op.text

    by_model = await client.get(
        f"{FEED_URL}?model=warehouse.shelf", headers={"accept": "text/html"}
    )
    assert "A-1" in by_model.text
    assert "Anvil" not in by_model.text

    bogus = await client.get(f"{FEED_URL}?source=bogus&op=bogus", headers={"accept": "text/html"})
    assert "No changes match these filters." not in bogus.text


async def test_feed_filters_by_date_window(client: httpx.AsyncClient) -> None:
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    await make_item("Anvil")

    today = datetime.now(timezone.utc).date()
    tomorrow = (today + timedelta(days=1)).isoformat()
    yesterday = (today - timedelta(days=1)).isoformat()

    included = await client.get(
        f"{FEED_URL}?since={yesterday}&until={today.isoformat()}",
        headers={"accept": "text/html"},
    )
    assert "Anvil" in included.text

    excluded = await client.get(f"{FEED_URL}?since={tomorrow}", headers={"accept": "text/html"})
    assert "No changes match these filters." in excluded.text


async def test_unviewable_model_filter_shows_nothing(client: httpx.AsyncClient) -> None:
    await make_item("Anvil")
    await make_user("auditor")
    await grant_permissions("auditor", "warehouse.view_item", "history.view_row_change")
    await login_session(client, "auditor")

    feed = await client.get(f"{FEED_URL}?model=warehouse.shelf", headers={"accept": "text/html"})
    assert feed.status_code == 200
    assert "No changes match these filters." in feed.text


# -- keyset paging -----------------------------------------------------------


async def test_feed_pages_with_a_before_cursor(client: httpx.AsyncClient) -> None:
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    item_id = await make_item("Anvil")
    async with session_scope() as db:
        for quantity in range(1, 56):
            item = await db.get(Item, item_id)
            assert item is not None
            item.quantity = quantity
            await db.flush()

    first = await client.get(FEED_URL, headers={"accept": "text/html"})
    assert first.status_code == 200
    assert "Older" in first.text
    assert "Newest" not in first.text

    async with session_scope() as db:
        cursor = await db.scalar(sa.select(RowChange.id).order_by(RowChange.id.desc()).offset(49))
    assert cursor is not None
    older = await client.get(f"{FEED_URL}?before={cursor}", headers={"accept": "text/html"})
    assert older.status_code == 200
    assert "Newest" in older.text


# -- change-set detail -------------------------------------------------------


async def test_change_set_detail_lists_the_operation(client: httpx.AsyncClient) -> None:
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    token = await csrf_for("alice")
    item_id = await make_item("Anvil")
    await client.post(
        f"{LIST_URL}/{item_id}",
        data={"csrf_token": token, "_action": "save", **item_form(quantity="9")},
        follow_redirects=False,
    )
    change_set_id = await latest_change_set()

    detail = await client.get(f"{FEED_URL}/{change_set_id}", headers={"accept": "text/html"})
    assert detail.status_code == 200
    assert "Change set" in detail.text
    assert "alice" in detail.text
    assert "Quantity" in detail.text


async def test_change_set_detail_hides_unviewable_models(client: httpx.AsyncClient) -> None:
    await make_shelf("A-1")
    change_set_id = await latest_change_set()
    await make_user("auditor")
    await grant_permissions("auditor", "warehouse.view_item", "history.view_row_change")
    await login_session(client, "auditor")

    hidden = await client.get(f"{FEED_URL}/{change_set_id}", headers={"accept": "text/html"})
    assert hidden.status_code == 404


async def test_change_set_detail_unknown_id_is_404(client: httpx.AsyncClient) -> None:
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    missing = await client.get(
        f"{FEED_URL}/00000000-0000-0000-0000-000000000000", headers={"accept": "text/html"}
    )
    assert missing.status_code == 404


@pytest.mark.parametrize("restriction", ["queryset", "object"])
async def test_row_visibility_covers_timeline_feed_change_set_export_and_revert(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    restriction: str,
) -> None:
    from obox_builder.contrib.admin.site import site

    await make_user("root", is_superuser=True)
    await login_session(client, "root")
    token = await csrf_for("root")
    hidden = await make_item("Restricted item")
    change_set = await latest_change_set()
    async with session_scope() as db:
        entry = await db.scalar(
            sa.select(RowChange).where(
                RowChange.model_label == "warehouse.item",
                RowChange.object_pk == str(hidden),
            )
        )
        assert entry is not None
        entry_id = entry.id
    admin = site.get_model_admin_for(Item)
    assert admin is not None
    if restriction == "queryset":
        monkeypatch.setattr(
            admin,
            "get_queryset",
            lambda ctx: ModelAdmin.get_queryset(admin, ctx).where(Item.id != hidden),
        )
    else:
        monkeypatch.setattr(
            admin, "has_view_permission", lambda ctx, obj=None: obj is None or obj.id != hidden
        )
    timeline = await client.get(f"{LIST_URL}/{hidden}/history")
    assert timeline.status_code == (404 if restriction == "queryset" else 403)
    for url in (FEED_URL, "/admin/history/export?format=ndjson"):
        response = await client.get(url)
        assert response.status_code == 200
        assert "Restricted item" not in response.text
    assert (await client.get(f"{FEED_URL}/{change_set}")).status_code == 404
    response = await client.post(
        f"{LIST_URL}/{hidden}/history/{entry_id}/revert", data={"csrf_token": token}
    )
    assert response.status_code == (404 if restriction == "queryset" else 403)
    async with session_scope() as db:
        assert await db.get(Item, hidden) is not None
