from __future__ import annotations

import httpx
import pytest
from obox_builder.conf import settings

from tests.admin.conftest import csrf_for, grant_permissions, login_session, make_user

HX = {"HX-Request": "true", "HX-Target": "main#admin-main", "HX-Request-Type": "partial"}


@pytest.mark.parametrize(
    ("headers", "fragment"),
    [
        ({}, False),
        (HX, True),
        ({**HX, "HX-Request": "false"}, False),
        ({**HX, "HX-Target": "body"}, False),
        ({**HX, "HX-Request-Type": "full"}, False),
        ({**HX, "HX-History-Restore-Request": "true"}, False),
    ],
)
async def test_full_pages_and_navigation_fragments(
    client: httpx.AsyncClient, headers: dict[str, str], fragment: bool
) -> None:
    await make_user("alice")
    await login_session(client, "alice")
    response = await client.get("/admin/", headers=headers)
    assert response.status_code == 200
    assert ("<!doctype html>" not in response.text) is fragment
    assert '<main id="admin-main"' in response.text
    assert "Dashboard &middot; obox admin" in response.text
    assert ("htmx-4.0.0.min.js" not in response.text) is fragment
    assert ('hx-swap-oob="outerHTML"' in response.text) is fragment
    assert response.headers["cache-control"] == "private, no-cache"
    assert set(response.headers["vary"].split(", ")) == {
        "HX-Request",
        "HX-Target",
        "HX-Request-Type",
        "HX-History-Restore-Request",
        "HX-Preloaded",
    }


async def test_navigation_can_be_disabled(client: httpx.AsyncClient) -> None:
    await make_user("alice")
    await login_session(client, "alice")
    settings.load().ADMIN_SMOOTH_NAVIGATION = False
    response = await client.get("/admin/", headers=HX)
    assert "<!doctype html>" in response.text
    assert "hx-boost:inherited" not in response.text
    assert "htmx-4.0.0.min.js" not in response.text


async def test_expired_navigation_redirects_the_document(client: httpx.AsyncClient) -> None:
    response = await client.get("/admin/?q=test", headers={**HX, "Accept": "text/html"})
    assert response.status_code == 401
    assert response.headers["hx-redirect"] == "/admin/login?next=/admin/%3Fq%3Dtest"
    assert "Max-Age=0" in response.headers["set-cookie"]
    normal = await client.get("/admin/", headers={"Accept": "text/html"})
    assert normal.status_code == 303


async def test_fragments_keep_macro_widgets_csrf_and_permissions(client: httpx.AsyncClient) -> None:
    await make_user("alice")
    token = await login_session(client, "alice")
    denied = await client.get("/admin/shop/product/add", headers={**HX, "Accept": "text/html"})
    assert denied.status_code == 403
    await grant_permissions("alice", "shop.add_product")
    response = await client.get("/admin/shop/product/add", headers=HX)
    assert response.status_code == 200
    assert "<!doctype html>" not in response.text
    assert 'name="name"' in response.text
    assert f'name="csrf_token" value="{await csrf_for("alice", token)}"' in response.text
    rejected = await client.post("/admin/shop/product/add", headers=HX, data={"name": "Test"})
    assert rejected.status_code == 403


async def test_preload_does_not_consume_pending_flash(client: httpx.AsyncClient) -> None:
    import sqlalchemy as sa
    from obox_builder.contrib.auth.models import Session
    from obox_builder.contrib.auth.sessions import hash_token
    from obox_builder.db.session import session_scope
    from obox_builder.http.messages import add_session_message

    await make_user("alice")
    token = await login_session(client, "alice")
    async with session_scope() as db:
        row = await db.scalar(sa.select(Session).where(Session.token_hash == hash_token(token)))
        assert row is not None
        add_session_message(row, "Saved successfully")
    preloaded = await client.get("/admin/", headers={**HX, "HX-Preloaded": "true"})
    assert "Saved successfully" not in preloaded.text
    actual = await client.get("/admin/", headers=HX)
    assert "Saved successfully" in actual.text
    again = await client.get("/admin/", headers=HX)
    assert "Saved successfully" not in again.text


async def test_invalid_form_does_not_push_history(client: httpx.AsyncClient) -> None:
    await make_user("alice", is_superuser=True)
    token = await login_session(client, "alice")
    response = await client.post(
        "/admin/shop/product/add",
        headers=HX,
        data={"csrf_token": await csrf_for("alice", token), "name": ""},
    )
    assert response.status_code == 200
    assert "invalid-feedback" in response.text
    assert response.headers["hx-push-url"] == "false"
    assert "<!doctype html>" not in response.text
