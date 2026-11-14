from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from obox_builder import API, Context, ModelBuilder, Obox, Principal, Settings
from obox_builder.auth import RBAC, Policy
from obox_builder.contrib.activity import Activity
from obox_builder.contrib.activity.models import Base as ActivityBase
from obox_builder.contrib.auth import (
    APIToken,
    APITokenMixin,
    Auth,
    ModelBackend,
    Role,
    SessionMixin,
    User,
    UserMixin,
)
from obox_builder.contrib.auth import Base as AuthBase
from obox_builder.contrib.auth.passwords import hash_password
from obox_builder.contrib.files import IMAGES, File, Files, attachments
from obox_builder.contrib.files.models import Base as FileBase
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

PASSWORD = "correct horse battery staple"  # noqa: S105


class Base(DeclarativeBase):
    pass


class Note(Base):
    __tablename__ = "token_notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    title_text: Mapped[str] = mapped_column(sa.String(40))
    owner_id: Mapped[int] = mapped_column(default=1)
    private_note: Mapped[str] = mapped_column(default="hidden")


class NotePolicy(RBAC[Note]):
    def scope(self, action: str, ctx: Context) -> sa.ColumnElement[bool]:
        return sa.and_(super().scope(action, ctx), Note.owner_id == ctx.principal.id)

    def presets(self, action: str, ctx: Context) -> dict[str, Any]:
        return {"owner_id": ctx.principal.id}


class NoteBuilder(ModelBuilder[Note]):
    slug = "notes"
    fields = ("id", "title_text", "owner_id")
    filter_fields = ("title_text",)
    sort_fields = ("id", "title_text")
    search_fields = ("title_text",)
    policy: Policy[Note] = NotePolicy("notes.records")
    permissions = ("comment", "history")


class Upload(Base):
    __tablename__ = "token_uploads"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(40))
    attachments: Mapped[list[File]] = attachments(types=IMAGES)


class UploadBuilder(ModelBuilder[Upload]):
    fields = ("id", "name", "attachments")
    slug = "uploads"
    policy = RBAC[Upload]()


class CustomAuthBase(DeclarativeBase):
    pass


class TokenUser(UserMixin, CustomAuthBase):
    __tablename__ = "custom_token_users"
    user_key: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    login: Mapped[str] = mapped_column(sa.String(100), unique=True)


class TokenSession(SessionMixin, CustomAuthBase):
    __tablename__ = "custom_token_sessions"
    user_id: Mapped[UUID] = mapped_column(sa.ForeignKey(TokenUser.user_key))


class PersonalToken(APITokenMixin, CustomAuthBase):
    __tablename__ = "custom_personal_tokens"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(sa.ForeignKey(TokenUser.user_key))


@asynccontextmanager
async def application(
    tmp_path: Path,
    *,
    tokens: bool = True,
    configured: type[ModelBuilder[Any]] = NoteBuilder,
    files: bool = False,
    host_identity: bool = False,
) -> AsyncIterator[tuple[Obox, httpx.AsyncClient]]:
    url = os.getenv("OBOX_TEST_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'auth.sqlite3'}")
    engine = create_async_engine(url)
    namespace = f"test_auth_api_{uuid4().hex}" if engine.dialect.name == "postgresql" else None
    if namespace:
        async with engine.begin() as connection:
            await connection.execute(sa.schema.CreateSchema(namespace))
        engine = engine.execution_options(schema_translate_map={None: namespace})
    app = Obox(
        FastAPI(),
        settings=Settings(
            session_factory=async_sessionmaker(engine, expire_on_commit=False),
            csrf_secret="x" * 32,
            principal_dependency=(lambda: Principal(id=1, is_authenticated=True, is_staff=True))
            if host_identity
            else None,
        ),
        plugins=[
            Auth(secure=False, api_tokens=tokens),
            Activity(),
            *([Files(root=tmp_path / "media")] if files else []),
        ],
    )
    app.register(configured)
    try:
        async with app.app.router.lifespan_context(app.app):
            async with engine.begin() as connection:
                for metadata in (
                    AuthBase.metadata,
                    ActivityBase.metadata,
                    Base.metadata,
                    *([FileBase.metadata] if files else []),
                ):
                    await connection.run_sync(metadata.create_all)
            password = await hash_password(PASSWORD)
            async with app.session_factory.begin() as db:
                role = Role(
                    name="Editor",
                    permissions=[
                        f"notes.records.{action}"
                        for action in (
                            "list",
                            "retrieve",
                            "create",
                            "update",
                            "delete",
                            "comment",
                            "history",
                        )
                    ]
                    + [
                        f"admin.{resource}.{action}"
                        for resource in ("notes", "uploads")
                        for action in ("list", "retrieve", "create", "update", "delete")
                    ],
                )
                db.add_all(
                    [
                        User(
                            username="alice", password_hash=password, department="Ops", roles=[role]
                        ),
                        User(
                            username="bob", password_hash=password, department="Ops", roles=[role]
                        ),
                        Note(title_text="Alice", owner_id=1),
                        Note(title_text="Bob", owner_id=2),
                    ]
                )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app.app), base_url="http://test"
            ) as client:
                yield app, client
    finally:
        if namespace:
            async with engine.begin() as connection:
                await connection.execute(sa.schema.DropSchema(namespace, cascade=True))
        await engine.dispose()


async def csrf(client: httpx.AsyncClient) -> str:
    response = await client.get("/api/auth/csrf")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "private, no-store"
    return str(response.json()["csrfToken"])


async def login(client: httpx.AsyncClient, username: str = "alice") -> dict[str, str]:
    response = await client.post(
        "/api/auth/login",
        json={"username": username, "password": PASSWORD},
        headers={"x-csrf-token": await csrf(client)},
    )
    assert response.status_code == 200, response.text
    assert "password" not in response.text
    return {"x-csrf-token": await csrf(client)}


async def issue(client: httpx.AsyncClient, scopes: list[str]) -> dict[str, Any]:
    response = await client.post(
        "/api/auth/tokens",
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
            assert (await client.get("/api/auth/me")).status_code == 401
            credentials = {"username": "alice", "password": PASSWORD}
            assert (await client.post("/api/auth/login", json=credentials)).status_code == 403
            before = await csrf(client)
            denied = await client.post(
                "/api/auth/login",
                json=credentials,
                headers={"x-csrf-token": before, "origin": "https://evil.test"},
            )
            assert denied.status_code == 403
            signed_in = await client.post(
                "/api/auth/login", json=credentials, headers={"x-csrf-token": before}
            )
            assert signed_in.status_code == 200
            assert signed_in.json()["id"] == "1"
            assert (await client.get("/api/auth/me")).json() == signed_in.json()
            assert "httponly" in signed_in.headers["set-cookie"].lower()
            stale = await client.post(
                "/api/notes", json={"titleText": "New"}, headers={"x-csrf-token": before}
            )
            assert stale.status_code == 403
            headers = {"x-csrf-token": await csrf(client)}
            assert (
                await client.post("/api/notes", json={"titleText": "New"}, headers=headers)
            ).status_code == 201
            cookies = httpx.Cookies(client.cookies)
            assert (await client.post("/api/auth/logout")).status_code == 403
            assert (await client.post("/api/auth/logout", headers=headers)).status_code == 204
            assert (await client.get("/api/auth/me")).status_code == 401
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app.app), base_url="http://test", cookies=cookies
            ) as replay:
                assert (await replay.get("/api/auth/me")).status_code == 401

    asyncio.run(scenario())


def test_api_token_scopes_hashing_row_scope_and_cookie_isolation(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with application(tmp_path) as (app, client):
            await login(client)
            issued = await issue(client, ["notes.records.list", "notes.records.retrieve"])
            token = issued.pop("token")
            assert token.startswith("obx_")
            listed = await client.get("/api/auth/tokens")
            assert listed.json() == {"items": [issued]}
            assert token not in listed.text
            async with app.session_factory() as db:
                stored = (await db.scalars(sa.select(APIToken))).one()
                assert stored.token_hash == hashlib.sha256(token.encode()).hexdigest()
                assert stored.token_hash != token
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app.app),
                base_url="http://test",
                headers={"authorization": "Bearer " + token},
            ) as bearer:
                rows = await bearer.get("/api/notes")
                assert [row["titleText"] for row in rows.json()["items"]] == ["Alice"]
                assert (await bearer.get("/api/notes/2")).status_code == 404
                assert (await bearer.get("/api/notes/1")).status_code == 200
                assert (
                    await bearer.post("/api/notes", json={"titleText": "Forbidden"})
                ).status_code == 403
                assert (await bearer.get("/api/notes/1/activity")).status_code == 403
                assert (await bearer.get("/api/auth/tokens")).status_code == 403
                assert (await bearer.get("/admin/notes")).status_code == 401
                metadata = (await bearer.get("/api/notes:describe")).json()
                assert metadata["operations"]["list"] is True
                assert metadata["operations"]["create"] is False
                assert {field["name"] for field in metadata["fields"]} == {
                    "id",
                    "titleText",
                    "ownerId",
                }
                assert "private_note" not in str(metadata)
                assert metadata["scopes"]["list"] == "notes.records.list"
                assert len((await bearer.get("/api:describe")).json()["resources"]) == 1
                async with app.session_factory.begin() as db:
                    role = (await db.scalars(sa.select(Role))).one()
                    role.permissions = []
                assert (await bearer.get("/api/notes")).status_code == 403
            invalid = await client.get("/api/notes", headers={"authorization": "Bearer invalid"})
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
                    "/api/auth/tokens/" + token["id"], headers={"x-csrf-token": await csrf(client)}
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
                    "/api/notes", headers={"authorization": "Bearer " + token["token"]}
                )
                assert response.status_code == 401

    asyncio.run(scenario())


def test_token_ownership_validation_and_staff_only_scope_enforcement(tmp_path: Path) -> None:
    class StaffBuilder(NoteBuilder):
        policy = RBAC[Note]()

    async def scenario() -> None:
        async with application(tmp_path, configured=StaffBuilder) as (app, client):
            await login(client)
            token = await issue(client, ["admin.notes.list"])
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app.app), base_url="http://test"
            ) as bearer:
                auth = {"authorization": "Bearer " + token["token"]}
                assert (await bearer.get("/api/notes", headers=auth)).status_code == 200
                assert (await bearer.get("/api/notes/1", headers=auth)).status_code == 403
            await login(client, "bob")
            assert (await client.get("/api/auth/tokens")).json() == {"items": []}
            denied = await client.delete(
                "/api/auth/tokens/" + token["id"], headers={"x-csrf-token": await csrf(client)}
            )
            assert denied.status_code == 404
            for scopes in (["unknown.notes.list"], ["admin.notes.list", "admin.notes.list"]):
                response = await client.post(
                    "/api/auth/tokens",
                    headers={"x-csrf-token": await csrf(client)},
                    json={
                        "name": "Invalid",
                        "scopes": scopes,
                        "expiresAt": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                    },
                )
                assert response.status_code == 422

    asyncio.run(scenario())


def test_api_tokens_are_opt_in(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with application(tmp_path, tokens=False) as (_, client):
            await login(client)
            assert (await client.get("/api/auth/tokens")).status_code == 404

    asyncio.run(scenario())


def test_file_bearer_tokens_enforce_scopes_and_api_field_projection(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with application(tmp_path, configured=UploadBuilder, files=True) as (app, client):
            await login(client)
            writable = await issue(client, ["admin.uploads.create", "admin.uploads.retrieve"])
            limited = await issue(client, ["admin.uploads.list"])
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app.app),
                base_url="http://test",
                follow_redirects=True,
            ) as bearer:
                auth = {"authorization": "Bearer " + writable["token"]}
                denied_auth = {"authorization": "Bearer " + limited["token"]}
                png = b"\x89PNG\r\n\x1a\ncontent"
                created = await bearer.post(
                    "/files",
                    headers=auth,
                    json={
                        "filename": "image.png",
                        "size": len(png),
                        "field": "token_uploads.attachments",
                    },
                )
                assert created.status_code == 201, created.text
                path = "/files/" + created.json()["id"]
                assert (await bearer.get(path + "/state", headers=denied_auth)).status_code == 404
                chunk_headers = {
                    "content-type": "application/offset+octet-stream",
                    "upload-offset": "0",
                }
                denied = await bearer.patch(
                    path, headers={**denied_auth, **chunk_headers}, content=png
                )
                assert denied.status_code == 404
                completed = await bearer.patch(path, headers={**auth, **chunk_headers}, content=png)
                assert completed.status_code == 200
                record = await bearer.post(
                    "/api/uploads",
                    headers=auth,
                    json={
                        "name": "Image",
                        "attachments": [created.json()["id"]],
                    },
                )
                assert record.status_code == 201, record.text
                download = await bearer.get(path, headers=auth)
                assert download.status_code == 200
                assert download.content == png
                assert (await bearer.get(path, headers=denied_auth)).status_code == 403
                invalid = await bearer.get(path, headers={"authorization": "Bearer invalid"})
                assert invalid.status_code == 401

    asyncio.run(scenario())


def test_file_bearer_upload_cannot_bypass_api_write_fields(tmp_path: Path) -> None:
    class Restricted(UploadBuilder):
        class API(API):
            write_fields = ("name",)

    async def scenario() -> None:
        async with application(tmp_path, configured=Restricted, files=True) as (app, client):
            await login(client)
            token = await issue(client, ["admin.uploads.create"])
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app.app), base_url="http://test"
            ) as bearer:
                response = await bearer.post(
                    "/files",
                    headers={"authorization": "Bearer " + token["token"]},
                    json={"filename": "image.png", "size": 8, "field": "token_uploads.attachments"},
                )
                assert response.status_code == 403

    asyncio.run(scenario())


def test_token_management_uses_its_own_authenticated_backend(tmp_path: Path) -> None:
    class StaffBuilder(NoteBuilder):
        policy = RBAC[Note]()

    async def scenario() -> None:
        async with application(tmp_path, configured=StaffBuilder, host_identity=True) as (
            _,
            client,
        ):
            response = await client.post(
                "/api/auth/tokens",
                json={
                    "name": "Denied",
                    "scopes": ["admin.notes.list"],
                    "expiresAt": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                },
            )
            assert response.status_code == 401

    asyncio.run(scenario())


def test_describe_narrows_fields_and_reports_effective_defaults(tmp_path: Path) -> None:
    class NarrowPolicy(NotePolicy):
        def read_fields(self, ctx: Context, *, obj: Note | None = None) -> frozenset[str]:
            return frozenset({"id", "title_text"})

    class NarrowBuilder(NoteBuilder):
        policy = NarrowPolicy("notes.records")

    async def scenario() -> None:
        async with application(tmp_path, configured=NarrowBuilder) as (app, client):
            assert (await client.get("/api:describe")).json() == {"resources": []}
            assert (await client.get("/api/notes:describe")).status_code == 404
            await login(client)
            response = await client.get("/api/notes:describe")
            assert response.status_code == 200
            assert response.headers["cache-control"] == "private, no-store"
            description = response.json()
            fields = {field["name"]: field for field in description["fields"]}
            assert set(fields) == {"id", "titleText"}
            assert fields["id"]["readOnly"] is True
            assert fields["id"]["schema"]["type"] == "string"
            assert fields["titleText"]["required"] is True
            assert fields["titleText"]["schema"]["maxLength"] == 40
            assert description["filterFields"] == ["titleText"]
            assert (
                description["defaultLimit"]
                is description["maxLimit"]
                is description["maxBatch"]
                is None
            )
            schema = app.app.openapi()
            assert schema["paths"]["/api/auth/login"]["post"]["security"] == []
            assert schema["paths"]["/api/notes"]["get"]["security"] == [
                {"cookieAuth": []},
                {"bearerAuth": []},
            ]
            assert schema["components"]["securitySchemes"]["bearerAuth"] == {
                "type": "http",
                "scheme": "bearer",
            }

    asyncio.run(scenario())


@pytest.mark.parametrize("expiry", ["2020-01-01T00:00:00Z", "2030-01-01T00:00:00"])
def test_token_expiry_is_required_to_be_future_and_timezone_aware(
    tmp_path: Path, expiry: str
) -> None:
    async def scenario() -> None:
        async with application(tmp_path) as (_, client):
            await login(client)
            response = await client.post(
                "/api/auth/tokens",
                headers={"x-csrf-token": await csrf(client)},
                json={"name": "Invalid", "scopes": ["notes.records.list"], "expiresAt": expiry},
            )
            assert response.status_code == 422
            assert response.json()["errors"][0]["pointer"] == "#/expiresAt"
            assert (await client.get("/api/auth/tokens")).json() == {"items": []}

    asyncio.run(scenario())


def test_custom_uuid_users_and_token_models_preserve_identity_and_expiry(tmp_path: Path) -> None:
    class StaffBuilder(NoteBuilder):
        policy = RBAC[Note]()

    async def scenario() -> None:
        engine = create_async_engine(
            os.getenv(
                "OBOX_TEST_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'custom.sqlite3'}"
            )
        )
        namespace = (
            f"test_custom_tokens_{uuid4().hex}" if engine.dialect.name == "postgresql" else None
        )
        if namespace:
            async with engine.begin() as connection:
                await connection.execute(sa.schema.CreateSchema(namespace))
            engine = engine.execution_options(schema_translate_map={None: namespace})
        factory = async_sessionmaker(engine, expire_on_commit=False)
        backend = ModelBackend(
            factory,
            user_model=TokenUser,
            session_model=TokenSession,
            token_model=PersonalToken,
            username_field="login",
        )
        app = Obox(
            FastAPI(),
            settings=Settings(session_factory=factory, csrf_secret="x" * 32),
            plugins=[Auth(backend=backend, secure=False, api_tokens=True)],
        )
        app.register(StaffBuilder)
        try:
            async with app.app.router.lifespan_context(app.app):
                async with engine.begin() as connection:
                    await connection.run_sync(CustomAuthBase.metadata.create_all)
                    await connection.run_sync(Base.metadata.create_all)
                key = uuid4()
                async with factory.begin() as db:
                    db.add(
                        TokenUser(
                            user_key=key,
                            login="alice",
                            password_hash=await hash_password(PASSWORD),
                            department="Ops",
                            is_superuser=True,
                        )
                    )
                    db.add(Note(title_text="Visible"))
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app.app), base_url="http://test"
                ) as client:
                    headers = await login(client)
                    assert (await client.get("/api/auth/me")).json()["id"] == str(key)
                    expiry = (datetime.now(UTC) + timedelta(hours=1)).astimezone(
                        timezone(timedelta(hours=7))
                    )
                    response = await client.post(
                        "/api/auth/tokens",
                        headers=headers,
                        json={
                            "name": "Custom user",
                            "scopes": ["admin.notes.list"],
                            "expiresAt": expiry.isoformat(),
                        },
                    )
                    assert response.status_code == 201, response.text
                    token = response.json()
                    assert token["expiresAt"].endswith("Z")
                    assert datetime.fromisoformat(token["expiresAt"]) == expiry
                    client.cookies.clear()
                    auth = {"authorization": "Bearer " + token["token"]}
                    assert (await client.get("/api/notes", headers=auth)).status_code == 200
                    assert (await client.get("/api/auth/me", headers=auth)).json()["id"] == str(key)
        finally:
            if namespace:
                async with engine.begin() as connection:
                    await connection.execute(sa.schema.DropSchema(namespace, cascade=True))
            await engine.dispose()

    asyncio.run(scenario())
