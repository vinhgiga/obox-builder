from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import sqlalchemy as sa
from fastapi import APIRouter, Depends, FastAPI
from obox.api import BaseApi
from obox.contrib.auth import APIToken, Auth, Role, User
from obox.contrib.auth.models import Base as AuthBase
from obox.contrib.auth.passwords import hash_password
from obox.core.exceptions import NotFound
from obox.runtime.config import RuntimeConfig
from obox.runtime.context import Context
from obox.runtime.runtime import _Runtime
from obox.web.routes import ApiRoute
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from tests.runtime import build_runtime

PASSWORD = "correct horse battery staple"  # noqa: S105


class Base(DeclarativeBase):
    pass


class Note(Base):
    __tablename__ = "token_notes"
    id: Mapped[int] = mapped_column(primary_key=True)
    title_text: Mapped[str] = mapped_column(sa.String(40))
    owner_id: Mapped[int] = mapped_column(default=1)


@asynccontextmanager
async def application(
    tmp_path: Path, *, tokens: bool = True
) -> AsyncIterator[tuple[_Runtime, httpx.AsyncClient]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'auth.sqlite3'}")
    app = build_runtime(
        FastAPI(),
        settings=RuntimeConfig(
            session_factory=async_sessionmaker(engine, expire_on_commit=False), csrf_secret="x" * 32
        ),
        features=[Auth(secure=False, api_tokens=tokens)],
    )
    router = APIRouter(route_class=ApiRoute)

    def dependency(action: str) -> Any:
        return Depends(BaseApi.permission_required("notes.records." + action))

    list_context, read_context, create_context, history_context = (
        dependency(action) for action in ("list", "retrieve", "create", "history")
    )

    @router.get("/api/admin/notes")
    async def notes(ctx: Context = list_context) -> Any:
        rows = await ctx.session.scalars(sa.select(Note).where(Note.owner_id == ctx.principal.id))
        return {"items": [{"id": str(row.id), "titleText": row.title_text} for row in rows]}

    @router.get("/api/admin/notes/{id}")
    async def note(id: int, ctx: Context = read_context) -> Any:
        row = await ctx.session.scalar(
            sa.select(Note).where(Note.owner_id == ctx.principal.id, Note.id == id)
        )
        if row is None:
            raise NotFound("Unknown note.")
        return {"id": str(row.id), "titleText": row.title_text}

    @router.post("/api/admin/notes", status_code=201)
    async def create(data: dict[str, Any], ctx: Context = create_context) -> Any:
        row = Note(title_text=data["titleText"], owner_id=ctx.principal.id)
        ctx.session.add(row)
        await ctx.session.flush()
        return {"id": str(row.id), "titleText": row.title_text}

    @router.get("/api/admin/notes/{id}/activity")
    async def history(id: int, ctx: Context = history_context) -> Any:
        return {}

    app.include_router(router, app_api=True)
    try:
        async with app.app.router.lifespan_context(app.app):
            async with engine.begin() as conn:
                await conn.run_sync(AuthBase.metadata.create_all)
                await conn.run_sync(Base.metadata.create_all)
            password = await hash_password(PASSWORD)
            async with app.session_factory.begin() as session:
                role = Role(
                    name="Editor",
                    permissions=[
                        "notes.records." + action
                        for action in ("list", "retrieve", "create", "history")
                    ],
                )
                session.add_all(
                    [
                        User(username="alice", password_hash=password, roles=[role]),
                        User(username="bob", password_hash=password, roles=[role]),
                        Note(title_text="Alice", owner_id=1),
                        Note(title_text="Bob", owner_id=2),
                    ]
                )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app.app), base_url="http://test"
            ) as client:
                yield app, client
    finally:
        await engine.dispose()


async def csrf(client: httpx.AsyncClient) -> str:
    response = await client.get("/v0/auth/csrf")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "private, no-store"
    return str(response.json()["csrfToken"])


async def login(client: httpx.AsyncClient, username: str = "alice") -> dict[str, str]:
    response = await client.post(
        "/v0/auth/login",
        json={"username": username, "password": PASSWORD},
        headers={"x-csrf-token": await csrf(client)},
    )
    assert response.status_code == 200, response.text
    assert "password" not in response.text
    return {"x-csrf-token": await csrf(client)}


async def issue(client: httpx.AsyncClient, scopes: list[str]) -> dict[str, Any]:
    response = await client.post(
        "/v0/auth/tokens",
        headers={"x-csrf-token": await csrf(client)},
        json={
            "name": "Integration",
            "scopes": scopes,
            "expiresAt": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )
    assert response.status_code == 201, response.text
    return cast(dict[str, Any], response.json())


def test_json_session_login_csrf_rotation_and_logout(tmp_path: Path) -> None:

    async def scenario() -> None:
        async with application(tmp_path) as (app, client):
            assert (await client.get("/v0/auth/me")).status_code == 401
            credentials = {"username": "alice", "password": PASSWORD}
            assert (await client.post("/v0/auth/login", json=credentials)).status_code == 403
            before = await csrf(client)
            denied = await client.post(
                "/v0/auth/login",
                json=credentials,
                headers={"x-csrf-token": before, "origin": "https://evil.test"},
            )
            assert denied.status_code == 403
            signed_in = await client.post(
                "/v0/auth/login", json=credentials, headers={"x-csrf-token": before}
            )
            assert signed_in.status_code == 200
            assert signed_in.json()["id"] == "1"
            assert (await client.get("/v0/auth/me")).json() == signed_in.json()
            assert "httponly" in signed_in.headers["set-cookie"].lower()
            stale = await client.post(
                "/api/admin/notes", json={"titleText": "New"}, headers={"x-csrf-token": before}
            )
            assert stale.status_code == 403
            headers = {"x-csrf-token": await csrf(client)}
            assert (
                await client.post("/api/admin/notes", json={"titleText": "New"}, headers=headers)
            ).status_code == 201
            cookies = httpx.Cookies(client.cookies)
            assert (await client.post("/v0/auth/logout")).status_code == 403
            assert (await client.post("/v0/auth/logout", headers=headers)).status_code == 204
            assert (await client.get("/v0/auth/me")).status_code == 401
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app.app), base_url="http://test", cookies=cookies
            ) as replay:
                assert (await replay.get("/v0/auth/me")).status_code == 401

    asyncio.run(scenario())


def test_api_token_scopes_hashing_row_scope_and_cookie_isolation(tmp_path: Path) -> None:

    async def scenario() -> None:
        async with application(tmp_path) as (app, client):
            await login(client)
            issued = await issue(client, ["notes.records.list", "notes.records.retrieve"])
            token = issued.pop("token")
            assert token.startswith("obx_")
            listed = await client.get("/v0/auth/tokens")
            assert listed.json() == {"items": [issued]}
            assert token not in listed.text
            async with app.session_factory() as db:
                stored = (await db.scalars(sa.select(APIToken))).one()
                assert stored.token_hash == hashlib.sha256(token.encode()).hexdigest()
                assert stored.token_hash != token
            writer = await issue(client, ["notes.records.create"])
            created = await client.post(
                "/api/admin/notes",
                json={"titleText": "Bearer write"},
                headers={"authorization": "Bearer " + writer["token"]},
            )
            assert created.status_code == 201
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app.app),
                base_url="http://test",
                headers={"authorization": "Bearer " + token},
            ) as bearer:
                rows = await bearer.get("/api/admin/notes")
                assert [row["titleText"] for row in rows.json()["items"]] == [
                    "Alice",
                    "Bearer write",
                ]
                assert (await bearer.get("/api/admin/notes/2")).status_code == 404
                assert (await bearer.get("/api/admin/notes/1")).status_code == 200
                assert (
                    await bearer.post("/api/admin/notes", json={"titleText": "Forbidden"})
                ).status_code == 403
                assert (await bearer.get("/api/admin/notes/1/activity")).status_code == 403
                assert (await bearer.get("/v0/auth/tokens")).status_code == 403
                assert (await bearer.get("/admin/admin/notes")).status_code == 404
                async with app.session_factory.begin() as db:
                    role = (await db.scalars(sa.select(Role))).one()
                    role.permissions = []
                assert (await bearer.get("/api/admin/notes")).status_code == 403
            invalid = await client.get(
                "/api/admin/notes", headers={"authorization": "Bearer invalid"}
            )
            assert invalid.status_code == 401
            assert invalid.headers["www-authenticate"] == 'Bearer realm="obox"'

    asyncio.run(scenario())


@pytest.mark.parametrize("reason", ["expired", "revoked", "password", "inactive"])
def test_api_tokens_recheck_expiry_revocation_password_and_user(
    tmp_path: Path, reason: str
) -> None:

    async def scenario() -> None:
        async with application(tmp_path) as (app, client):
            await login(client)
            token = await issue(client, ["notes.records.list"])
            if reason == "revoked":
                response = await client.delete(
                    "/v0/auth/tokens/" + token["id"], headers={"x-csrf-token": await csrf(client)}
                )
                assert response.status_code == 204
            else:
                async with app.session_factory.begin() as db:
                    if reason == "expired":
                        row = (await db.scalars(sa.select(APIToken))).one()
                        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
                    else:
                        user = await db.get(User, 1)
                        assert user is not None
                        if reason == "password":
                            user.password_hash = await hash_password(PASSWORD + " new")
                        else:
                            user.is_active = False
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app.app), base_url="http://test"
            ) as bearer:
                response = await bearer.get(
                    "/api/admin/notes", headers={"authorization": "Bearer " + token["token"]}
                )
                assert response.status_code == 401

    asyncio.run(scenario())


def test_api_tokens_are_opt_in(tmp_path: Path) -> None:

    async def scenario() -> None:
        async with application(tmp_path, tokens=False) as (_, client):
            await login(client)
            assert (await client.get("/v0/auth/tokens")).status_code == 404

    asyncio.run(scenario())


@pytest.mark.parametrize("expiry", ["2020-01-01T00:00:00Z", "2030-01-01T00:00:00"])
def test_token_expiry_is_required_to_be_future_and_timezone_aware(
    tmp_path: Path, expiry: str
) -> None:

    async def scenario() -> None:
        async with application(tmp_path) as (_, client):
            await login(client)
            response = await client.post(
                "/v0/auth/tokens",
                headers={"x-csrf-token": await csrf(client)},
                json={"name": "Invalid", "scopes": ["notes.records.list"], "expiresAt": expiry},
            )
            assert response.status_code == 422
            assert response.json()["errors"][0]["pointer"] == "#/expiresAt"
            assert (await client.get("/v0/auth/tokens")).json() == {"items": []}

    asyncio.run(scenario())
