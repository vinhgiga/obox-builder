"""Admin integration: history card and timeline for the bespoke auth pages."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import sqlalchemy as sa
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


@pytest.fixture
async def client(history_env: None) -> AsyncIterator[httpx.AsyncClient]:
    from obox_builder.db import dispose, get_engine
    from obox_builder.db.base import ModelBase

    async with get_engine().begin() as connection:
        await connection.run_sync(ModelBase.metadata.drop_all)
        await connection.run_sync(ModelBase.metadata.create_all)
    await ensure_permissions()
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http
    await dispose()


async def make_role(name: str = "Editors") -> int:
    from obox_builder.contrib.auth.models import Role

    async with session_scope() as db:
        role = Role(name=name)
        db.add(role)
        await db.flush()
        return role.id


# -- users -------------------------------------------------------------------


async def test_user_edit_shows_history_card_and_timeline(client: httpx.AsyncClient) -> None:
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    target_id = await make_user("bob")

    form = await client.get(f"/admin/users/{target_id}", headers={"accept": "text/html"})
    assert form.status_code == 200
    assert "Full history" in form.text
    assert f'href="/admin/users/{target_id}/history"' in form.text

    listing = await client.get("/admin/users", headers={"accept": "text/html"})
    assert listing.status_code == 200
    assert f'data-history-url="/admin/users/{target_id}/history"' in listing.text

    timeline = await client.get(
        f"/admin/users/{target_id}/history", headers={"accept": "text/html"}
    )
    assert timeline.status_code == 200
    assert "User history" in timeline.text
    assert "bob" in timeline.text
    assert "create" in timeline.text


async def test_user_history_requires_the_history_permission(client: httpx.AsyncClient) -> None:
    await make_user("editor")
    await grant_permissions("editor", "auth.view_user", "auth.change_user")
    await login_session(client, "editor")
    target_id = await make_user("bob")

    form = await client.get(f"/admin/users/{target_id}", headers={"accept": "text/html"})
    assert form.status_code == 200
    assert "Full history" not in form.text

    listing = await client.get("/admin/users", headers={"accept": "text/html"})
    assert listing.status_code == 200
    assert "data-history-url" not in listing.text

    denied = await client.get(f"/admin/users/{target_id}/history", headers={"accept": "text/html"})
    assert denied.status_code == 403


async def test_user_history_permission_alone_is_not_enough(client: httpx.AsyncClient) -> None:
    await make_user("auditor")
    await grant_permissions("auditor", "auth.change_user", "history.view_row_change")
    await login_session(client, "auditor")
    target_id = await make_user("bob")

    denied = await client.get(f"/admin/users/{target_id}/history", headers={"accept": "text/html"})
    assert denied.status_code == 403


async def test_user_history_404_for_unknown_row(client: httpx.AsyncClient) -> None:
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")

    missing = await client.get("/admin/users/99999/history", headers={"accept": "text/html"})
    assert missing.status_code == 404


async def test_bespoke_user_save_records_the_actor(client: httpx.AsyncClient) -> None:
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    target_id = await make_user("bob")
    token = await csrf_for("alice")

    response = await client.post(
        f"/admin/users/{target_id}",
        data={
            "csrf_token": token,
            "email": "bob@example.com",
            "full_name": "Bob Builder",
            "is_active": "true",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    async with session_scope() as db:
        row = await db.scalar(
            sa.select(RowChange)
            .where(RowChange.model_label == "auth.user", RowChange.op == "update")
            .order_by(RowChange.id.desc())
        )
        assert row is not None
        assert row.actor_label == "alice"
        assert row.source == "admin_ui"
        assert row.changes["fields"]["full_name"] == ["", "Bob Builder"]


# -- roles -------------------------------------------------------------------


async def test_role_edit_shows_history_card_and_timeline(client: httpx.AsyncClient) -> None:
    await make_user("alice", is_superuser=True)
    await login_session(client, "alice")
    role_id = await make_role()

    form = await client.get(f"/admin/roles/{role_id}", headers={"accept": "text/html"})
    assert form.status_code == 200
    assert "Full history" in form.text
    assert f'href="/admin/roles/{role_id}/history"' in form.text

    listing = await client.get("/admin/roles", headers={"accept": "text/html"})
    assert listing.status_code == 200
    assert f'data-history-url="/admin/roles/{role_id}/history"' in listing.text

    timeline = await client.get(f"/admin/roles/{role_id}/history", headers={"accept": "text/html"})
    assert timeline.status_code == 200
    assert "Role history" in timeline.text
    assert "Editors" in timeline.text
    assert "create" in timeline.text


async def test_role_history_requires_the_history_permission(client: httpx.AsyncClient) -> None:
    await make_user("editor")
    await grant_permissions("editor", "auth.view_role", "auth.change_role")
    await login_session(client, "editor")
    role_id = await make_role()

    form = await client.get(f"/admin/roles/{role_id}", headers={"accept": "text/html"})
    assert form.status_code == 200
    assert "Full history" not in form.text

    denied = await client.get(f"/admin/roles/{role_id}/history", headers={"accept": "text/html"})
    assert denied.status_code == 403
