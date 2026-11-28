from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

import httpx
import pytest
import sqlalchemy as sa
from fastapi import APIRouter, Depends, FastAPI, Request
from obox import ModelResource
from obox.api import ApiRequest, ApiResponse, Context, ModelApi, api_context
from obox.core.exceptions import ImproperlyConfigured, PermissionDenied
from obox.core.principal import Principal
from obox.runtime.config import RuntimeConfig
from obox.runtime.runtime import _Runtime
from pydantic import Field, StringConstraints
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from tests.runtime import collection_filter, resource_for, sessions_for
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


class Artist(Base):
    __tablename__ = "model_api_artists"
    artist_id: Mapped[int] = mapped_column(primary_key=True)


class Album(Base):
    __tablename__ = "model_api_albums"
    album_id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(sa.String(160))
    artist_id: Mapped[int] = mapped_column(sa.ForeignKey(Artist.artist_id))


class AlbumWithRelationship(Base):
    __tablename__ = "model_api_albums_with_relationship"
    album_id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(sa.String(160))
    artist_id: Mapped[int] = mapped_column(sa.ForeignKey(Artist.artist_id))
    artist: Mapped[Artist] = relationship()


Title = Annotated[str, StringConstraints(min_length=1, max_length=64)]


class PostWrite(ApiRequest):
    title_text: Title = Field(alias="titleText")
    summary_text: str | None = None
    category_id: int | None = Field(default=None, alias="categoryId")


class PostResource(ModelResource[Post]):
    model = Post
    write_schema = PostWrite

    def get_queryset(self) -> sa.Select[tuple[Post]]:
        return super().get_queryset().where(Post.owner_id == self.principal.id)

    def prepare_write(self, values: dict[str, Any], obj: Post | None, actor: Any) -> dict[str, Any]:
        return {**values, "owner_id": self.principal.id} if obj is None else values


class Posts(ModelApi[Post]):
    resource = PostResource
    prefix = "/posts"
    search_fields: Sequence[str] = ("title_text",)
    ordering_fields: Sequence[str] = ("title_text",)


class Albums(ModelApi[Album]):
    resource = resource_for(Album)
    actions: tuple[Literal["retrieve"]] = ("retrieve",)
    permissions = MappingProxyType({"retrieve": "tests.view_post"})


class AlbumsWithRelationship(ModelApi[AlbumWithRelationship]):
    resource = resource_for(AlbumWithRelationship)
    actions: tuple[Literal["retrieve"]] = ("retrieve",)
    permissions = MappingProxyType({"retrieve": "tests.view_post"})


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
    if who == "album-viewer":
        return Principal(
            id=1,
            is_authenticated=True,
            permissions=frozenset(
                {"tests.view_post", "tests.view_album", "tests.view_albumwithrelationship"}
            ),
        )
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
                ("PATCH", POSTS + "/1"),
                ("DELETE", POSTS + "/1"),
            ]:
                body = {"titleText": "Test"} if method != "DELETE" else None
                assert (
                    await client.request(method, path, headers=VIEWER, json=body)
                ).status_code == 403

            created = await client.post(
                POSTS,
                headers=EDITOR,
                json={
                    "titleText": "Hello World",
                    "summaryText": None,
                    "categoryId": 1,
                },
            )
            assert created.status_code == 201, created.text
            data = created.json()
            stored_id = data["id"]
            assert data == {
                "id": stored_id,
                "titleText": "Hello World",
                "summaryText": None,
                "categoryId": 1,
            }
            detail = f"{POSTS}/{stored_id}"
            assert created.headers["location"] == detail
            assert (await client.get(detail, headers=VIEWER)).json() == data
            for method in ("GET", "PATCH", "DELETE"):
                body = {"titleText": "Test"} if method == "PATCH" else None
                assert (
                    await client.request(method, detail, headers=OTHER, json=body)
                ).status_code == 404

            changed = await client.patch(detail, headers=EDITOR, json={"summaryText": "Changed"})
            assert changed.json() == {**data, "summaryText": "Changed"}
            assert (await client.patch(detail, headers=EDITOR, json={})).json() == changed.json()
            assert (await client.put(detail, headers=EDITOR, json={})).status_code == 405
            cleared = await client.patch(detail, headers=EDITOR, json={"summaryText": None})
            assert cleared.json() == data
            assert (
                await client.patch(detail, headers=EDITOR, json={"titleText": None})
            ).status_code == 422
            for invalid_body in ({"title_text": "old"}, {"ownerId": 2}, {"id": 9}):
                assert (
                    await client.patch(detail, headers=EDITOR, json=invalid_body)
                ).status_code == 422
            bad_fk = await client.patch(detail, headers=EDITOR, json={"categoryId": 999})
            assert bad_fk.status_code == 422
            assert bad_fk.json()["errors"][0]["pointer"] == "#/categoryId"
            assert (await client.get(POSTS + "/bad", headers=EDITOR)).status_code == 422
            deleted = await client.delete(detail, headers=EDITOR)
            assert deleted.status_code == 204
            assert deleted.content == b""
            assert (await client.get(detail, headers=EDITOR)).status_code == 404

            schema = runtime.app.openapi()
            member = schema["paths"][f"{POSTS}/{{id}}"]
            assert member.keys() == {"get", "patch", "delete"}
            assert member["patch"]["parameters"][0]["schema"]["type"] == "integer"
            assert "requestBody" in schema["paths"][POSTS]["post"]
            assert "Problem" in schema["components"]["schemas"]
            assert runtime.permissions.keys() >= WRITE

    asyncio.run(scenario())


def test_album_api_response_uses_columns_with_or_without_relationship(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with database(tmp_path / "album-api.sqlite3", (Base.metadata,)) as sessions:
            async with sessions.begin() as session:
                session.add(Artist(artist_id=1))
                await session.flush()
                session.add_all(
                    [
                        Album(
                            album_id=1,
                            title="For Those About To Rock We Salute You",
                            artist_id=1,
                        ),
                        AlbumWithRelationship(
                            album_id=1,
                            title="For Those About To Rock We Salute You",
                            artist_id=1,
                        ),
                    ]
                )

            expected = {
                "albumId": 1,
                "title": "For Those About To Rock We Salute You",
                "artistId": 1,
            }
            for api in (Albums, AlbumsWithRelationship):
                app = FastAPI()
                _Runtime(
                    app,
                    settings=RuntimeConfig(
                        session_factory=sessions, principal_dependency=principal
                    ),
                )
                app.include_router(api().build_router("/v0/album"))
                async with (
                    app.router.lifespan_context(app),
                    httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app), base_url="http://test"
                    ) as client,
                ):
                    response = await client.get(
                        "/v0/album/1", headers={"x-test-user": "album-viewer"}
                    )
                    assert response.status_code == 200, response.text
                    assert response.json() == expected

    asyncio.run(scenario())


def test_list_scope_search_sort_and_pagination(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with application(tmp_path, Posts().build_router()) as (_, client):
            for name, headers in [("A%", EDITOR), ("B%", EDITOR), ("C", EDITOR), ("D%", OTHER)]:
                result = await client.post(POSTS, headers=headers, json={"titleText": name})
                assert result.status_code == 201, result.text
            first = await client.get(
                POSTS,
                headers=EDITOR,
                params={
                    "q": "%",
                    "ordering": "-title_text",
                    "limit": 1,
                    "includeTotal": "true",
                },
            )
            assert first.status_code == 200, first.text
            assert first.json() == {
                "items": [{"id": 2, "titleText": "B%", "summaryText": None, "categoryId": None}],
                "limit": 1,
                "offset": 0,
                "hasMore": True,
                "total": 2,
            }
            second = await client.get(
                POSTS + "?q=%25&ordering=-title_text&limit=1&offset=1", headers=EDITOR
            )
            assert not second.json()["hasMore"]
            assert second.json()["total"] is None
            assert second.json()["items"][0]["titleText"] == "A%"
            capped = await client.get(f"{POSTS}?limit=101", headers=EDITOR)
            assert capped.status_code == 200
            assert capped.json()["limit"] == 100
            for query in (
                "limit=0",
                "offset=-1",
                "ordering=owner_id",
                "ordering=title_text.asc",
                "limit=1&limit=2",
                "include_total=true",
                "unknown=x",
            ):
                assert (await client.get(f"{POSTS}?{query}", headers=EDITOR)).status_code == 422

    asyncio.run(scenario())


def test_group_shares_list_permissions_and_scopes(tmp_path: Path) -> None:
    class Resource(PostResource):
        group_fields = ("summary_text",)

        def get_queryset(self) -> sa.Select[tuple[Post]]:
            return super().get_queryset().where(Post.id >= 2).order_by(Post.id).limit(2)

        async def check_permission(self, action: str, obj: Post | None = None) -> None:
            if action == "list" and self.principal.id == 2:
                raise PermissionDenied("Listing is unavailable.")
            await super().check_permission(action, obj)

    class ScopedPosts(Posts):
        resource = Resource
        permissions = MappingProxyType({"list": "tests.add_record"})

    class JoinedResource(PostResource):
        def get_queryset(self) -> sa.Select[tuple[Post]]:
            repeated = sa.union_all(
                sa.select(sa.literal(2).label("post_id")), sa.select(sa.literal(2))
            ).subquery()
            return super().get_queryset().join(repeated, repeated.c.post_id == Post.id)

    class JoinedPosts(Posts):
        resource = JoinedResource
        prefix = "/joined-posts"

    async def scenario() -> None:
        router = APIRouter()
        router.include_router(ScopedPosts().build_router())
        router.include_router(JoinedPosts().build_router())
        async with application(tmp_path, router) as (runtime, client):
            async with runtime.session_factory.begin() as session:
                session.add_all(
                    Post(
                        id=i,
                        title_text=f"Post {i}",
                        owner_id=owner,
                        summary_text=sa.null() if summary is None else summary,
                    )
                    for i, owner, summary in [
                        (1, 1, "Before"),
                        (2, 1, ""),
                        (3, 2, "Other"),
                        (4, 1, None),
                        (5, 1, "After"),
                    ]
                )
            path = POSTS + ":groups"
            params = {"group": "summary_text", "includeTotal": "true"}
            for headers, status in [({}, 401), (VIEWER, 403), (OTHER, 403)]:
                assert (await client.get(POSTS, headers=headers)).status_code == status
                assert (
                    await client.get(path, headers=headers, params=params)
                ).status_code == status
            allowed = await client.get(path, headers=EDITOR, params=params)
            assert allowed.status_code == 200, allowed.text
            assert allowed.json()["items"] == [
                {"key": {"summaryText": ""}, "count": 1},
                {"key": {"summaryText": None}, "count": 1},
            ]
            assert allowed.json()["total"] == 2
            for url, query in [
                (path, {"group": "summary_text", "q": "5"}),
                (path, {"group": "summary_text", "filter": collection_filter(("id", "is", 5))}),
                (POSTS, {"filter": collection_filter(("id", "is", 5))}),
            ]:
                response = await client.get(url, headers=EDITOR, params=query)
                assert response.status_code == 200, response.text
                assert response.json()["items"] == []
            excluded = await client.get(path, headers=EDITOR, params={"group": "title_text"})
            assert excluded.status_code == 422
            assert excluded.json()["code"] == "invalid_query"
            joined = await client.get(
                "/api/joined-posts:groups", headers=EDITOR, params={"group": "title_text"}
            )
            assert joined.status_code == 200, joined.text
            assert joined.json()["items"] == [{"key": {"titleText": "Post 2"}, "count": 1}]

    asyncio.run(scenario())


def test_overrides_keep_scoped_lookup_and_rollback(tmp_path: Path) -> None:
    class DeferredReference(Base):
        __tablename__ = "model_api_deferred_references"
        id: Mapped[int] = mapped_column(primary_key=True)
        category_id: Mapped[int] = mapped_column(
            sa.ForeignKey(Category.id, deferrable=True, initially="DEFERRED")
        )

    class FailingResource(PostResource):
        async def perform_create(self, values: dict[str, Any]) -> Post:
            row = await super().perform_create(values)
            if row.title_text == "handler failure":
                await self._session().flush()
                raise RuntimeError("internal operation failed")
            if row.title_text == "commit failure":
                if self._session().get_bind().dialect.name == "postgresql":
                    self._session().add(DeferredReference(category_id=999))
                    await self._session().flush()
                    return row

                @sa.event.listens_for(self._session().sync_session, "before_commit", once=True)
                def fail_commit(*args: Any) -> None:
                    raise sa.exc.IntegrityError(None, None, RuntimeError("constraint failure"))

            return row

    class FailingPosts(Posts):
        resource = FailingResource

    async def scenario() -> None:
        async with application(tmp_path, FailingPosts().build_router()) as (_, client):
            for name, expected in [
                ("handler failure", 500),
                ("commit failure", 409),
                ("OK", 201),
                ("OK", 409),
            ]:
                response = await client.post(POSTS, headers=EDITOR, json={"titleText": name})
                assert response.status_code == expected, response.text
                if expected != 201:
                    assert response.headers["content-type"] == "application/problem+json"
                    assert "failure" not in response.json()["detail"]
            rows = (await client.get(POSTS, headers=EDITOR)).json()["items"]
            assert [row["titleText"] for row in rows] == ["OK"]

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
        resource = resource_for(Record, write_schema=RecordWrite)
        prefix = "/records"
        actions = ("create", "retrieve")
        permissions = MappingProxyType({"create": "tests.add_post"})
        read_schema = RecordRead

    def records_router() -> APIRouter:
        router = APIRouter()

        @router.get("/records")
        async def public() -> dict[str, bool]:
            return {"public": True}

        @router.get("/records/stats")
        async def stats(
            ctx: Annotated[Context, Depends(api_context(permission="tests.view_post", csrf=True))],
        ) -> dict[str, bool]:
            return {"allowed": True}

        router.include_router(Records().build_router())
        return router

    async def scenario() -> None:
        async with application(tmp_path, records_router()) as (runtime, client):
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
    with pytest.raises(ImproperlyConfigured):
        resource_for(Post, fields=("owner_id",))()
    with pytest.raises(ImproperlyConfigured):
        resource_for(
            Post, write_schema=type("BadWrite", (ApiRequest,), {"__annotations__": {"id": int}})
        )()

    app = FastAPI()
    runtime = _Runtime(
        app, settings=RuntimeConfig(session_factory=sessions_for("sqlite+aiosqlite://"))
    )
    app.include_router(Posts().build_router(), prefix="/api")
    app.add_api_route(POSTS + "/{other_id}", lambda: None, methods=["GET"])
    with pytest.raises(ImproperlyConfigured, match="Duplicate API route"):
        runtime.check()

    grouped = FastAPI()
    group_runtime = _Runtime(
        grouped, settings=RuntimeConfig(session_factory=sessions_for("sqlite+aiosqlite://"))
    )
    grouped.include_router(Posts().build_router(), prefix="/api")
    grouped.add_api_route(POSTS + ":groups", lambda: None, methods=["GET"])
    with pytest.raises(ImproperlyConfigured, match="Duplicate API route"):
        group_runtime.check()
