from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any
from uuid import UUID, uuid4

import httpx
import pytest
import sqlalchemy as sa
from fastapi import APIRouter, Depends, FastAPI, Request
from obox import ModelResource
from obox.api import ApiRequest, ApiResponse, BaseApi, Context, ModelApi
from obox.core.exceptions import ImproperlyConfigured
from obox.core.principal import Principal
from obox.runtime.config import RuntimeConfig
from obox.runtime.runtime import _Runtime
from pydantic import Field, StringConstraints
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from tests.storage import database

POSTS = "/api/posts"
WRITE = frozenset(
    {
        "tests.view_post",
        "tests.add_post",
        "tests.change_post",
        "tests.delete_post",
    }
)
VIEWER = {"x-test-user": "viewer"}
EDITOR = {"x-test-user": "editor"}
OTHER = {"x-test-user": "other"}


class Base(DeclarativeBase):
    pass


class Category(Base):
    __tablename__ = "model_api_categories"
    id: Mapped[int] = mapped_column(primary_key=True)


class Post(Base):
    __tablename__ = "model_api_posts"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(info={"obox": {"expose": False}})
    title_text: Mapped[str] = mapped_column(sa.String(64), unique=True)
    summary_text: Mapped[str | None] = mapped_column(server_default="database default")
    category_id: Mapped[int | None] = mapped_column(sa.ForeignKey(Category.id))


Title = Annotated[str, StringConstraints(min_length=1, max_length=64)]


class PostRead(ApiResponse):
    id: int
    title_text: str = Field(alias="headlineText")
    summary_text: str | None
    category_id: int | None


class PostWrite(ApiRequest):
    title_text: Title = Field(alias="headlineText")
    summary_text: str | None = None
    category_id: int | None = Field(default=None, alias="sectionId")


class PostPatch(PostWrite):
    title_text: Title = Field(default_factory=str, alias="headlineText")


class PostResource(ModelResource[Post]):
    model = Post

    def get_queryset(self) -> sa.Select[tuple[Post]]:
        return super().get_queryset().where(Post.owner_id == self.principal.id)

    def prepare_write(self, values: dict[str, Any], obj: Post | None, actor: Any) -> dict[str, Any]:
        return {**values, "owner_id": self.principal.id} if obj is None else values


class Posts(ModelApi[Post]):
    resource = PostResource
    prefix = "/posts"
    read_schema: type[ApiResponse] | None = PostRead
    create_schema: type[ApiRequest] | None = PostWrite
    update_schema = PostWrite
    patch_schema: type[ApiRequest] | None = PostPatch
    search_fields = ordering_fields = ("title_text",)


def principal(request: Request) -> Principal:
    who = request.headers.get("x-test-user")
    if who in {"editor", "other"}:
        return Principal(
            id=1 if who == "editor" else 2,
            is_authenticated=True,
            permissions=WRITE | {"tests.add_record", "tests.view_record"},
        )
    if who == "viewer":
        return Principal(id=1, is_authenticated=True, permissions=frozenset({"tests.view_post"}))
    if who in {"member", "inactive"}:
        return Principal(id=1, is_authenticated=True, is_active=who == "member")
    return Principal()


@asynccontextmanager
async def application(
    tmp_path: Path, router: APIRouter
) -> AsyncIterator[tuple[_Runtime, httpx.AsyncClient]]:
    async with database(tmp_path / "api.sqlite3", (Base.metadata,)) as sessions:
        app = FastAPI()
        runtime = _Runtime(
            app, settings=RuntimeConfig(session_factory=sessions, principal_dependency=principal)
        )
        app.include_router(router, prefix="/api")
        async with app.router.lifespan_context(app):
            async with sessions.begin() as session:
                session.add(Category(id=1))
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                yield runtime, client


def test_crud_permissions_visibility_and_wire_contract(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with application(tmp_path, Posts().build_router()) as (runtime, client):
            for headers, expected in [
                ({}, 401),
                ({"x-test-user": "inactive"}, 401),
                ({"x-test-user": "member"}, 403),
                (VIEWER, 200),
            ]:
                assert (await client.get(POSTS, headers=headers)).status_code == expected
            assert (await client.head(POSTS)).status_code == 401
            for method, path in [
                ("POST", POSTS),
                ("PUT", POSTS + "/1"),
                ("PATCH", POSTS + "/1"),
                ("DELETE", POSTS + "/1"),
            ]:
                body = {"headlineText": "Test"} if method != "DELETE" else None
                assert (
                    await client.request(method, path, headers=VIEWER, json=body)
                ).status_code == 403

            created = await client.post(
                POSTS,
                headers=EDITOR,
                json={
                    "headlineText": "Hello World",
                    "summaryText": None,
                    "sectionId": 1,
                },
            )
            assert created.status_code == 201, created.text
            data = created.json()
            stored_id = data["id"]
            assert data == {
                "id": stored_id,
                "headlineText": "Hello World",
                "summaryText": None,
                "categoryId": 1,
            }
            detail = f"{POSTS}/{stored_id}"
            assert created.headers["location"] == detail
            assert (await client.get(detail, headers=VIEWER)).json() == data
            for method in ("GET", "PUT", "PATCH", "DELETE"):
                body = {"headlineText": "Test"} if method in {"PUT", "PATCH"} else None
                assert (
                    await client.request(method, detail, headers=OTHER, json=body)
                ).status_code == 404

            changed = await client.patch(detail, headers=EDITOR, json={"summaryText": "Changed"})
            assert changed.json() == {**data, "summaryText": "Changed"}
            assert (await client.patch(detail, headers=EDITOR, json={})).json() == changed.json()
            assert (await client.put(detail, headers=EDITOR, json={})).status_code == 422
            replaced = await client.put(detail, headers=EDITOR, json={"headlineText": "New"})
            assert replaced.json() == {
                "id": stored_id,
                "headlineText": "New",
                "summaryText": None,
                "categoryId": None,
            }
            assert (
                await client.patch(detail, headers=EDITOR, json={"headlineText": None})
            ).status_code == 422
            for invalid_body in ({"title_text": "old"}, {"ownerId": 2}, {"id": 9}):
                assert (
                    await client.patch(detail, headers=EDITOR, json=invalid_body)
                ).status_code == 422
            bad_fk = await client.patch(detail, headers=EDITOR, json={"sectionId": 999})
            assert bad_fk.status_code == 422
            assert bad_fk.json()["errors"][0]["pointer"] == "#/sectionId"
            assert (await client.get(POSTS + "/bad", headers=EDITOR)).status_code == 422
            deleted = await client.delete(detail, headers=EDITOR)
            assert deleted.status_code == 204
            assert deleted.content == b""
            assert (await client.get(detail, headers=EDITOR)).status_code == 404

            schema = runtime.app.openapi()
            assert (
                schema["paths"][f"{POSTS}/{{id}}"]["patch"]["parameters"][0]["schema"]["type"]
                == "integer"
            )
            assert "requestBody" in schema["paths"][POSTS]["post"]
            assert "Problem" in schema["components"]["schemas"]
            assert runtime.permissions.keys() >= WRITE

    asyncio.run(scenario())


def test_list_scope_search_sort_and_pagination(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with application(tmp_path, Posts().build_router()) as (_, client):
            for name, headers in [("A%", EDITOR), ("B%", EDITOR), ("C", EDITOR), ("D%", OTHER)]:
                result = await client.post(POSTS, headers=headers, json={"headlineText": name})
                assert result.status_code == 201, result.text
            first = await client.get(
                POSTS,
                headers=EDITOR,
                params={
                    "search": "%",
                    "order": "headlineText.desc",
                    "limit": 1,
                    "includeTotal": "true",
                },
            )
            assert first.status_code == 200, first.text
            assert first.json() == {
                "items": [{"id": 2, "headlineText": "B%", "summaryText": None, "categoryId": None}],
                "limit": 1,
                "offset": 0,
                "hasMore": True,
                "total": 2,
            }
            second = await client.get(
                POSTS + "?search=%25&order=headlineText.desc&limit=1&offset=1", headers=EDITOR
            )
            assert not second.json()["hasMore"]
            assert second.json()["total"] is None
            assert second.json()["items"][0]["headlineText"] == "A%"
            for query in (
                "limit=0",
                "limit=101",
                "offset=-1",
                "order=ownerId",
                "order=title_text.asc",
                "limit=1&limit=2",
                "include_total=true",
                "unknown=x",
            ):
                assert (await client.get(f"{POSTS}?{query}", headers=EDITOR)).status_code == 422

    asyncio.run(scenario())


def test_overrides_keep_scoped_lookup_and_rollback(tmp_path: Path) -> None:
    class DeferredReference(Base):
        __tablename__ = "model_api_deferred_references"
        id: Mapped[int] = mapped_column(primary_key=True)
        category_id: Mapped[int] = mapped_column(
            sa.ForeignKey(Category.id, deferrable=True, initially="DEFERRED")
        )

    class FailingPosts(Posts):
        async def create(self, ctx: Context, values: dict[str, Any]) -> Post:
            row = await super().create(ctx, values)
            if row.title_text == "handler failure":
                await ctx.session.flush()
                raise RuntimeError("internal operation failed")
            if row.title_text == "commit failure":
                if ctx.session.get_bind().dialect.name == "postgresql":
                    ctx.session.add(DeferredReference(category_id=999))
                    await ctx.session.flush()
                    return row

                @sa.event.listens_for(ctx.session.sync_session, "before_commit", once=True)
                def fail_commit(*args: Any) -> None:
                    raise sa.exc.IntegrityError(None, None, RuntimeError("constraint failure"))

            return row

    async def scenario() -> None:
        async with application(tmp_path, FailingPosts().build_router()) as (_, client):
            for name, expected in [
                ("handler failure", 500),
                ("commit failure", 409),
                ("OK", 201),
                ("OK", 409),
            ]:
                response = await client.post(POSTS, headers=EDITOR, json={"headlineText": name})
                assert response.status_code == expected, response.text
                if expected != 201:
                    assert response.headers["content-type"] == "application/problem+json"
                    assert "failure" not in response.json()["detail"]
            rows = (await client.get(POSTS, headers=EDITOR)).json()["items"]
            assert [row["headlineText"] for row in rows] == ["OK"]

    asyncio.run(scenario())


def test_custom_route_permissions_and_uuid(tmp_path: Path) -> None:
    class Record(Base):
        __tablename__ = "model_api_uuids"
        record_key: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
        title_text: Mapped[str]

    class RecordRead(ApiResponse):
        record_key: UUID
        title_text: str

    class RecordWrite(ApiRequest):
        title_text: str

    class Records(ModelApi[Record]):
        model = Record
        prefix = "/records"
        include_router_methods = ("create", "retrieve")
        permissions = MappingProxyType({"create": "tests.add_post"})
        read_schema = RecordRead
        create_schema = RecordWrite

        def configure_router(self, router: APIRouter) -> None:
            @router.get("")
            async def public() -> dict[str, bool]:
                return {"public": True}

            @router.get("/stats")
            async def stats(
                ctx: Annotated[Context, Depends(BaseApi.permission_required("tests.view_post"))],
            ) -> dict[str, bool]:
                return {"allowed": True}

    async def scenario() -> None:
        async with application(tmp_path, Records().build_router()) as (runtime, client):
            path = "/api/records"
            assert (await client.get(path)).json() == {"public": True}
            assert (await client.get(path + "/stats")).status_code == 401
            assert (await client.get(path + "/stats", headers=VIEWER)).json() == {"allowed": True}
            created = await client.post(path, headers=EDITOR, json={"titleText": "UUID"})
            assert created.status_code == 201, created.text
            key = UUID(created.json()["recordKey"])
            assert (await client.get(f"{path}/{key}")).status_code == 401
            assert (
                await client.get(f"{path}/{key}", headers={"x-test-user": "member"})
            ).status_code == 403
            assert (await client.get(f"{path}/{key}", headers=EDITOR)).json() == created.json()
            assert (await client.delete(f"{path}/{key}", headers=EDITOR)).status_code == 405
            operations = runtime.app.openapi()["paths"][path + "/{id}"]
            assert list(operations) == ["get"]
            assert operations["get"]["parameters"][0]["schema"]["format"] == "uuid"

    asyncio.run(scenario())


def test_rejects_bad_schema_and_duplicate_routes() -> None:
    class HiddenRead(ApiResponse):
        owner_id: int

    class ForbiddenWrite(ApiRequest):
        id: int

    class MissingBody(Posts):
        update_schema = None
        include_router_methods = ("update",)

    class HiddenField(Posts):
        read_schema = HiddenRead

    class ReadOnlyField(Posts):
        create_schema = ForbiddenWrite

    class Duplicate(Posts):
        def configure_router(self, router: APIRouter) -> None:
            router.add_api_route("/{other_id}", lambda: None, methods=["GET"])

    for api, error in [
        (MissingBody, "update_schema"),
        (HiddenField, "owner_id"),
        (ReadOnlyField, "Invalid field 'id'"),
        (Duplicate, "Duplicate API route"),
    ]:
        with pytest.raises(ImproperlyConfigured, match=error):
            api().build_router()

    app = FastAPI()
    runtime = _Runtime(app, settings=RuntimeConfig(database_url="sqlite+aiosqlite://"))
    app.include_router(Posts().build_router(), prefix="/api")
    app.add_api_route(POSTS + "/{other_id}", lambda: None, methods=["GET"])
    with pytest.raises(ImproperlyConfigured, match="Duplicate API route"):
        runtime.check()
