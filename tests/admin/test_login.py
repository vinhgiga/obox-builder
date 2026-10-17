from __future__ import annotations

import httpx
import pytest
import sqlalchemy as sa

from tests.admin.conftest import csrf_for, form_login, login_session, make_user

pytestmark = pytest.mark.usefixtures("admin_env")


async def test_login_page_renders(client: httpx.AsyncClient) -> None:
    response = await client.get("/admin/login")
    assert response.status_code == 200
    assert "Log in to your account" in response.text


async def test_login_rejects_bad_credentials(client: httpx.AsyncClient) -> None:
    await make_user("alice")
    response = await form_login(client, "alice", "wrong-password")
    assert response.status_code == 200
    assert "Invalid username or password." in response.text
    assert "set-cookie" not in response.headers


async def test_login_success_sets_cookie_and_redirects(client: httpx.AsyncClient) -> None:
    await make_user("alice")
    response = await form_login(client, "alice", "s3cure-password")
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/"
    assert "obox_sid" in response.headers["set-cookie"]

    via_cookie = await client.get("/admin/", headers={"accept": "text/html"})
    assert via_cookie.status_code == 200


async def test_login_honors_safe_next(client: httpx.AsyncClient) -> None:
    await make_user("alice")
    response = await form_login(client, "alice", "s3cure-password", next="/admin/users")
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/users"


async def test_login_ignores_external_next(client: httpx.AsyncClient) -> None:
    await make_user("alice")
    response = await form_login(
        client, "alice", "s3cure-password", next="https://evil.example.com/phish"
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/"


async def test_login_rejects_cross_origin(client: httpx.AsyncClient) -> None:
    await make_user("alice")
    response = await client.post(
        "/admin/login",
        data={"username": "alice", "password": "s3cure-password"},
        headers={"origin": "https://evil.example.com"},
        follow_redirects=False,
    )
    assert response.status_code == 403


async def test_login_cycles_session_on_relogin(client: httpx.AsyncClient) -> None:
    from obox_builder.conf import settings
    from obox_builder.contrib.auth.models import Session as AuthSession
    from obox_builder.db.session import session_scope

    await make_user("alice")
    first = await form_login(client, "alice", "s3cure-password")
    first_token = first.cookies.get(settings.SESSION_COOKIE_NAME)
    assert first_token is not None

    second = await form_login(client, "alice", "s3cure-password")
    second_token = second.cookies.get(settings.SESSION_COOKIE_NAME)
    assert second_token is not None
    assert second_token != first_token

    from obox_builder.contrib.auth.sessions import hash_token

    async with session_scope() as db:
        rows = list(await db.scalars(sa.select(AuthSession)))
        assert len(rows) == 1
        assert rows[0].token_hash == hash_token(second_token)


async def test_authenticated_login_redirects(client: httpx.AsyncClient) -> None:
    await make_user("alice")
    await login_session(client, "alice")
    response = await client.get("/admin/login", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/"


async def test_logout_requires_csrf(client: httpx.AsyncClient) -> None:
    await make_user("alice")
    await login_session(client, "alice")
    response = await client.post("/admin/logout", data={}, follow_redirects=False)
    assert response.status_code == 403


async def test_logout_deletes_session(client: httpx.AsyncClient) -> None:
    from obox_builder.contrib.auth.models import Session as AuthSession
    from obox_builder.db.session import session_scope

    await make_user("alice")
    await login_session(client, "alice")
    token = await csrf_for("alice")
    response = await client.post(
        "/admin/logout", data={"csrf_token": token}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"
    async with session_scope() as db:
        assert await db.scalar(sa.select(AuthSession)) is None
