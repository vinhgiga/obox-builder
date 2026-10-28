"""Admin audit export (Phase 3): CSV/NDJSON, filters, guards, permissions."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx
import pytest
from obox_builder.contrib.admin import ModelAdmin
from obox_builder.db.session import session_scope

from tests.admin.conftest import (
    build_app,
    ensure_permissions,
    grant_permissions,
    login_session,
    make_user,
)
from tests.history.test_admin_timeline import make_item
from tests.support.apps.warehouse.models import Contact, Item, Tag

EXPORT_URL = "/admin/history/export"


def _register_admins() -> None:
    from obox_builder.contrib.admin.site import site

    admins = (
        (Item, "ItemAdmin", {"list_display": ("id", "name", "quantity"), "ordering": ("-id",)}),
        (
            Contact,
            "ContactAdmin",
            {"list_display": ("id", "name", "email"), "ordering": ("-id",)},
        ),
        (Tag, "TagAdmin", {"list_display": ("id", "label"), "ordering": ("-id",)}),
    )
    for model, name, attrs in admins:
        if site.get_model_admin_for(model) is None:
            site.register(model, type(name, (ModelAdmin,), attrs))


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


async def test_csv_export_downloads_with_guard_and_filters(client: httpx.AsyncClient) -> None:
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    await make_item("=SUM(A1)")

    response = await client.get(f"{EXPORT_URL}?format=csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert 'attachment; filename="history-' in response.headers["content-disposition"]
    lines = response.text.splitlines()
    assert lines[0] == (
        "id,change_set_id,occurred_at,op,source,actor,model,object_pk,object_repr,changes"
    )
    assert "'=SUM(A1)" in response.text  # formula injection guard

    filtered = await client.get(f"{EXPORT_URL}?format=csv&model=warehouse.item&op=create")
    assert filtered.status_code == 200
    assert "warehouse.item" in filtered.text
    assert "auth.user" not in filtered.text
    assert "warehouse.contact" not in filtered.text


async def test_ndjson_export_is_one_json_object_per_line(client: httpx.AsyncClient) -> None:
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    await make_item("Anvil")

    response = await client.get(f"{EXPORT_URL}?format=ndjson&model=warehouse.item")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    records = [json.loads(line) for line in response.text.splitlines()]
    assert records
    assert all(record["model"] == "warehouse.item" for record in records)
    assert "Name:  -> Anvil" in records[-1]["changes"]


async def test_export_masks_fields_and_marks_them(client: httpx.AsyncClient) -> None:
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    async with session_scope() as db:
        contact = Contact(name="Ada", email="ada@example.com")
        db.add(contact)
        await db.flush()

    response = await client.get(f"{EXPORT_URL}?format=csv&model=warehouse.contact")
    assert response.status_code == 200
    assert "a***@example.com" in response.text
    assert "ada@example.com" not in response.text
    assert "(masked)" in response.text


async def test_export_requires_the_export_permission(client: httpx.AsyncClient) -> None:
    await make_user("viewer")
    await grant_permissions("viewer", "history.rowChange.read")
    await login_session(client, "viewer")
    denied = await client.get(EXPORT_URL)
    assert denied.status_code == 403

    await grant_permissions("viewer", "history.rowChange.export")
    allowed = await client.get(EXPORT_URL)
    assert allowed.status_code == 200
    assert "auth.user" not in allowed.text


async def test_unknown_export_format_is_a_bad_request(client: httpx.AsyncClient) -> None:
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    response = await client.get(f"{EXPORT_URL}?format=xlsx")
    assert response.status_code == 400
    assert "Unknown export format" in response.text


async def test_feed_links_to_the_export_with_current_filters(
    client: httpx.AsyncClient,
) -> None:
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    page = await client.get("/admin/history?model=warehouse.item", headers={"accept": "text/html"})
    assert page.status_code == 200
    assert f'href="{EXPORT_URL}?model=warehouse.item&amp;format=csv"' in page.text
