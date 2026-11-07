from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from obox_builder import Obox
from obox_builder.conf import Settings
from obox_builder.contrib.auth import Auth, Base, ModelBackend, Session, User
from obox_builder.contrib.auth.passwords import hash_password

PASSWORD = "correct horse battery staple"  # noqa: S105


def make_app(path: Path, max_age: int | None = None) -> Obox:
    auth = Auth(secure=False) if max_age is None else Auth(secure=False, max_age=max_age)
    return Obox(
        FastAPI(),
        settings=Settings(database_url=f"sqlite+aiosqlite:///{path}", csrf_secret="x" * 32),
        plugins=[auth],
    )


async def seed(app: Obox) -> None:
    assert app.database.engine is not None
    async with app.database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with app.session_factory.begin() as db:
        db.add(User(username="alice", password_hash=await hash_password(PASSWORD)))


async def login(client: httpx.AsyncClient, password: str = PASSWORD) -> httpx.Response:
    page = await client.get("/vi/auth/login")
    assert page.status_code == 200
    match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert match is not None
    return await client.post(
        "/vi/auth/login",
        data={"username": "alice", "password": password, "csrf_token": match[1]},
        follow_redirects=False,
    )


def test_failed_login_returns_authentication_challenge(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = make_app(tmp_path / "login.sqlite3")
        async with app.app.router.lifespan_context(app.app):
            await seed(app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app.app),
                base_url="http://test",
                follow_redirects=True,
            ) as client:
                response = await login(client, "wrong password")
                assert response.status_code == 401
                assert response.headers["www-authenticate"] == (
                    'Cookie realm="obox", cookie-name=obox_session, form-action="/vi/auth/login"'
                )

    asyncio.run(scenario())


@pytest.mark.parametrize("max_age", [None, 3600])
def test_session_lifetime_and_restart(tmp_path: Path, max_age: int | None) -> None:
    async def scenario() -> None:
        path = tmp_path / "sessions.sqlite3"
        app = make_app(path, max_age)
        ttl = 14 * 24 * 60 * 60 if max_age is None else max_age
        now = datetime.now(UTC)
        with patch("obox_builder.contrib.auth.backends.datetime") as clock:
            clock.now.return_value = now
            async with app.app.router.lifespan_context(app.app):
                await seed(app)
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app.app),
                    base_url="http://test",
                    follow_redirects=True,
                ) as client:
                    response = await login(client)
                    assert response.status_code == 303
                    cookie: SimpleCookie = SimpleCookie()
                    for header in response.headers.get_list("set-cookie"):
                        cookie.load(header)
                    session_cookie = cookie["obox_session"]
                    assert int(session_cookie["max-age"]) == ttl
                    assert session_cookie["httponly"]
                    assert session_cookie["samesite"] == "lax"
                    cookies = httpx.Cookies(client.cookies)
                async with app.session_factory() as db:
                    expiry = await db.scalar(sa.select(Session.expires_at))
                    assert expiry is not None
                    assert expiry.replace(tzinfo=UTC) == now + timedelta(seconds=ttl)

            restarted = make_app(path, max_age)
            async with (
                restarted.app.router.lifespan_context(restarted.app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=restarted.app),
                    base_url="http://test",
                    cookies=cookies,
                ) as client,
            ):
                for age in (0, ttl - 1):
                    clock.now.return_value = now + timedelta(seconds=age)
                    response = await client.get("/vi/auth/profile")
                    assert response.status_code == 200
                    assert "alice" in response.text
                    assert "set-cookie" not in response.headers
                async with restarted.session_factory() as db:
                    assert await db.scalar(sa.select(Session.expires_at)) == expiry
                clock.now.return_value = now + timedelta(seconds=ttl)
                response = await client.get("/vi/auth/profile")
                assert response.status_code == 303
                assert response.headers["location"] == "/vi/auth/login"

    asyncio.run(scenario())


def test_clear_browser_data_then_refresh_and_login(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = make_app(tmp_path / "clear-data.sqlite3")
        async with app.app.router.lifespan_context(app.app):
            await seed(app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app.app),
                base_url="http://test",
                follow_redirects=True,
            ) as client:
                assert (await login(client)).status_code == 303
                assert (await client.get("/vi/auth/profile")).status_code == 200
                previous = client.cookies["obox_session"]
                client.cookies.clear()
                page = await client.get("/vi/auth/profile")
                assert page.status_code == 200
                assert page.url.path == "/vi/auth/login"
                assert "obox_login" in client.cookies
                assert "obox_session" not in client.cookies
                assert "Authentication is required." not in page.text
                assert (await login(client)).status_code == 303
                assert client.cookies["obox_session"] != previous
                profile = await client.get("/vi/auth/profile")
                assert profile.status_code == 200
                assert "alice" in profile.text

    asyncio.run(scenario())


def test_direct_backend_login_defaults_to_fourteen_days(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = make_app(tmp_path / "backend.sqlite3")
        async with app.app.router.lifespan_context(app.app):
            await seed(app)
            backend = ModelBackend(app.session_factory)
            now = datetime.now(UTC)
            with patch("obox_builder.contrib.auth.backends.datetime") as clock:
                clock.now.return_value = now
                token = await backend.login("alice", PASSWORD)
                assert token is not None
                clock.now.return_value = now + timedelta(days=13)
                assert (await backend.resolve(token)).is_authenticated
                clock.now.return_value = now + timedelta(days=14)
                assert not (await backend.resolve(token)).is_authenticated

    asyncio.run(scenario())
