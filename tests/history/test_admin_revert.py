"""Admin revert/restore (Phase 3): variants, conflicts, permissions, deleted rows."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from obox_builder.contrib.admin import ModelAdmin
from obox_builder.contrib.history.models import RowChange
from obox_builder.contrib.history.service import model_label
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
from tests.history.test_admin_timeline import item_form, make_item
from tests.support.apps.warehouse.models import Contact, Item, Tag

LIST_URL = "/admin/warehouse/item"
CONTACT_URL = "/admin/warehouse/contact"


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


async def last_entry_id(model: type[Any], pk: Any, op: str | None = None) -> int:
    async with session_scope() as db:
        stmt = sa.select(sa.func.max(RowChange.id)).where(
            RowChange.model_label == model_label(model),
            RowChange.object_pk == str(pk),
        )
        if op is not None:
            stmt = stmt.where(RowChange.op == op)
        entry_id = await db.scalar(stmt)
    assert entry_id is not None
    return entry_id


async def entry_row(entry_id: int) -> RowChange:
    async with session_scope() as db:
        row = await db.get(RowChange, entry_id)
    assert row is not None
    return row


async def edit_item(client: httpx.AsyncClient, username: str, pk: int, **values: str) -> None:
    token = await csrf_for(username)
    response = await client.post(
        f"{LIST_URL}/{pk}",
        data={"csrf_token": token, "_action": "save", **item_form(**values)},
        follow_redirects=False,
    )
    assert response.status_code == 303


async def revert(client: httpx.AsyncClient, username: str, pk: Any, entry_id: int, **extra: str):
    token = await csrf_for(username)
    return await client.post(
        f"{LIST_URL}/{pk}/history/{entry_id}/revert",
        data={"csrf_token": token, **extra},
        follow_redirects=False,
    )


# -- update revert -----------------------------------------------------------


async def test_revert_update_restores_values_and_records_a_new_entry(
    client: httpx.AsyncClient,
) -> None:
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    item_id = await make_item("Anvil")
    await edit_item(client, "alice", item_id, quantity="9", name="Anvil II")
    update_id = await last_entry_id(Item, item_id, "update")

    response = await revert(client, "alice", item_id, update_id)
    assert response.status_code == 303
    assert response.headers["location"] == f"{LIST_URL}/{item_id}/history"

    async with session_scope() as db:
        item = await db.get(Item, item_id)
        assert item is not None
        assert item.quantity == 0
        assert item.name == "Anvil"

    row = await entry_row(await last_entry_id(Item, item_id))
    assert row.source == "revert"
    assert row.reverts_id == update_id
    assert row.actor_label == "alice"
    assert row.changes["fields"]["quantity"] == [9, 0]
    assert row.changes["fields"]["name"] == ["Anvil II", "Anvil"]


async def test_revert_update_requires_restore_and_change_permissions(
    client: httpx.AsyncClient,
) -> None:
    item_id = await make_item("Anvil")
    await make_user("editor")
    await grant_permissions(
        "editor",
        "warehouse.item.read",
        "warehouse.item.update",
        "history.rowChange.read",
    )
    await login_session(client, "editor")
    await edit_item(client, "editor", item_id, quantity="9")
    update_id = await last_entry_id(Item, item_id, "update")

    timeline = await client.get(f"{LIST_URL}/{item_id}/history", headers={"accept": "text/html"})
    assert timeline.status_code == 200
    assert "Revert" not in timeline.text

    denied = await revert(client, "editor", item_id, update_id)
    assert denied.status_code == 403

    await grant_permissions("editor", "history.rowChange.restore")
    restored = await revert(client, "editor", item_id, update_id)
    assert restored.status_code == 303


async def test_revert_conflict_lists_current_value_and_force_overrides(
    client: httpx.AsyncClient,
) -> None:
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    item_id = await make_item("Anvil")
    await edit_item(client, "alice", item_id, quantity="9")
    update_id = await last_entry_id(Item, item_id, "update")

    async with session_scope() as db:
        item = await db.get(Item, item_id)
        assert item is not None
        item.quantity = 5

    conflicted = await revert(client, "alice", item_id, update_id)
    assert conflicted.status_code == 409
    assert "Revert conflict" in conflicted.text
    assert "Revert anyway" in conflicted.text
    assert f'action="{LIST_URL}/{item_id}/history/{update_id}/revert"' in conflicted.text

    async with session_scope() as db:
        item = await db.get(Item, item_id)
        assert item is not None
        assert item.quantity == 5  # nothing changed without force

    forced = await revert(client, "alice", item_id, update_id, force="1")
    assert forced.status_code == 303
    async with session_scope() as db:
        item = await db.get(Item, item_id)
        assert item is not None
        assert item.quantity == 0


# -- create/delete variants --------------------------------------------------


async def test_revert_create_deletes_the_row_and_keeps_the_timeline(
    client: httpx.AsyncClient,
) -> None:
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    token = await csrf_for("alice")
    response = await client.post(
        f"{LIST_URL}/add",
        data={"csrf_token": token, "_action": "save", **item_form()},
        follow_redirects=False,
    )
    assert response.status_code == 303
    async with session_scope() as db:
        item_id = await db.scalar(sa.select(sa.func.max(Item.id)))
    assert item_id is not None
    create_id = await last_entry_id(Item, item_id, "create")

    reverted = await revert(client, "alice", item_id, create_id)
    assert reverted.status_code == 303
    async with session_scope() as db:
        assert await db.get(Item, item_id) is None
    delete_row = await entry_row(await last_entry_id(Item, item_id))
    assert delete_row.op == "delete"
    assert delete_row.source == "revert"
    assert delete_row.reverts_id == create_id

    listing = await client.get(LIST_URL, headers={"accept": "text/html"})
    assert ">Anvil<" not in listing.text

    form = await client.get(f"{LIST_URL}/{item_id}", headers={"accept": "text/html"})
    assert form.status_code == 404

    timeline = await client.get(f"{LIST_URL}/{item_id}/history", headers={"accept": "text/html"})
    assert timeline.status_code == 200
    assert "has been deleted" in timeline.text
    assert 'href="/admin/warehouse/item"' in timeline.text


async def test_revert_create_needs_delete_permission(client: httpx.AsyncClient) -> None:
    item_id = await make_item("Anvil")
    create_id = await last_entry_id(Item, item_id, "create")
    await make_user("keeper")
    await grant_permissions(
        "keeper",
        "warehouse.item.read",
        "warehouse.item.update",
        "warehouse.item.create",
        "history.rowChange.read",
        "history.rowChange.restore",
    )
    await login_session(client, "keeper")

    response = await revert(client, "keeper", item_id, create_id)
    assert response.status_code == 403
    assert "warehouse.item.delete" in response.text


async def test_revert_delete_restores_the_same_primary_key(client: httpx.AsyncClient) -> None:
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    item_id = await make_item("Anvil", quantity=7)
    token = await csrf_for("alice")
    deleted = await confirmed_delete(
        client,
        f"{LIST_URL}/{item_id}/delete",
        data={"csrf_token": token},
    )
    assert deleted.status_code == 303
    delete_id = await last_entry_id(Item, item_id, "delete")

    response = await revert(client, "alice", item_id, delete_id)
    assert response.status_code == 303, response.text
    async with session_scope() as db:
        item = await db.get(Item, item_id)
        assert item is not None
        assert item.name == "Anvil"
        assert item.quantity == 7
        assert item.version == 1  # a restored row is a fresh INSERT
    row = await entry_row(await last_entry_id(Item, item_id))
    assert row.op == "create"
    assert row.source == "revert"
    assert row.reverts_id == delete_id


async def test_revert_delete_is_blocked_when_the_pk_is_in_use(client: httpx.AsyncClient) -> None:
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    item_id = await make_item("Anvil")
    token = await csrf_for("alice")
    await confirmed_delete(client, f"{LIST_URL}/{item_id}/delete", data={"csrf_token": token})
    delete_id = await last_entry_id(Item, item_id, "delete")
    # Reuse the primary key for a different row.
    async with session_scope() as db:
        db.add(Item(id=item_id, name="Bolt", sku="SKU-BOLT"))
        await db.flush()

    response = await revert(client, "alice", item_id, delete_id)
    assert response.status_code == 409
    assert "primary key is in use" in response.text


# -- blocked fields ----------------------------------------------------------


async def test_revert_is_blocked_for_masked_and_secret_and_m2m_changes(
    client: httpx.AsyncClient,
) -> None:
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")

    async with session_scope() as db:
        contact = Contact(name="Ada", email="ada@example.com")
        db.add(contact)
        await db.flush()
        contact_id = contact.id

    async with session_scope() as db:
        refreshed = await db.get(Contact, contact_id)
        assert refreshed is not None
        refreshed.email = "ada.lovelace@example.com"
    masked_entry = await last_entry_id(Contact, contact_id, "update")
    token = await csrf_for("alice")
    blocked = await client.post(
        f"{CONTACT_URL}/{contact_id}/history/{masked_entry}/revert",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert blocked.status_code == 409
    assert "Cannot be restored" in blocked.text
    assert "masked value." in blocked.text

    # M2M changes cannot be restored either.
    item_id = await make_item("Anvil")
    async with session_scope() as db:
        tag = Tag(label="heavy")
        item = await db.get(Item, item_id)
        assert item is not None
        await db.refresh(item, ["tags"])
        item.tags.append(tag)
    m2m_entry = await last_entry_id(Item, item_id)
    denied = await revert(client, "alice", item_id, m2m_entry)
    assert denied.status_code == 409
    assert "relation changes cannot be restored." in denied.text


# -- change sets -------------------------------------------------------------


async def _shared_change_set(first_id: int, second_id: int) -> str:
    async with session_scope() as db:
        first = await db.get(Item, first_id)
        second = await db.get(Item, second_id)
        assert first is not None
        assert second is not None
        first.quantity = 1
        second.quantity = 2
    async with session_scope() as db:
        change_set_id = await db.scalar(
            sa.select(RowChange.change_set_id).order_by(RowChange.id.desc()).limit(1)
        )
    assert change_set_id is not None
    return str(change_set_id)


async def test_change_set_revert_restores_all_entries_in_one_transaction(
    client: httpx.AsyncClient,
) -> None:
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    first_id = await make_item("Anvil")
    second_id = await make_item("Bolt")
    change_set_id = await _shared_change_set(first_id, second_id)
    async with session_scope() as db:
        newest = await db.scalar(sa.select(sa.func.max(RowChange.id)))
    assert newest is not None

    page = await client.get(f"/admin/history/{change_set_id}", headers={"accept": "text/html"})
    assert page.status_code == 200
    assert "Revert change set" in page.text

    token = await csrf_for("alice")
    response = await client.post(
        f"/admin/history/{change_set_id}/revert",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/admin/history/{change_set_id}"

    async with session_scope() as db:
        first = await db.get(Item, first_id)
        second = await db.get(Item, second_id)
        assert first is not None
        assert second is not None
        assert first.quantity == 0
        assert second.quantity == 0
        rows = list(
            await db.scalars(
                sa.select(RowChange)
                .where(RowChange.source == "revert", RowChange.id > newest)
                .order_by(RowChange.id)
            )
        )
    assert {row.object_pk for row in rows} == {str(first_id), str(second_id)}
    assert all(row.reverts_id == newest for row in rows)


async def test_change_set_revert_is_all_or_nothing_on_conflict(
    client: httpx.AsyncClient,
) -> None:
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    first_id = await make_item("Anvil")
    second_id = await make_item("Bolt")
    change_set_id = await _shared_change_set(first_id, second_id)

    async with session_scope() as db:
        first = await db.get(Item, first_id)
        assert first is not None
        first.quantity = 5

    token = await csrf_for("alice")
    response = await client.post(
        f"/admin/history/{change_set_id}/revert",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 409
    assert "Revert conflict" in response.text

    async with session_scope() as db:
        first = await db.get(Item, first_id)
        second = await db.get(Item, second_id)
        assert first is not None
        assert second is not None
        assert first.quantity == 5
        assert second.quantity == 2
