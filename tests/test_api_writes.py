from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from obox_builder import Context, DomainEvent, ModelBuilder, Obox, Principal, Settings, View
from obox_builder.auth import ScopedIdentity, StaffOnly
from obox_builder.builder import surfaces
from obox_builder.contrib.activity import Activity
from obox_builder.contrib.activity.models import Base as ActivityBase
from obox_builder.contrib.activity.models import Event
from obox_builder.contrib.idempotency import Idempotency
from obox_builder.contrib.idempotency.models import Base as ReceiptBase
from obox_builder.contrib.idempotency.models import Receipt
from obox_builder.errors import ImproperlyConfigured, PermissionDenied
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


links = sa.Table(
    "write_links",
    Base.metadata,
    sa.Column("parent_id", sa.ForeignKey("write_parents.id"), primary_key=True),
    sa.Column("tag_id", sa.ForeignKey("write_tags.id"), primary_key=True),
)


class Parent(Base):
    __tablename__ = "write_parents"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(50), unique=True)
    score: Mapped[int | None]
    active: Mapped[bool | None]
    version: Mapped[int] = mapped_column(default=1)
    tags: Mapped[list[Tag]] = relationship(secondary=links, back_populates="parents")
    children: Mapped[list[Child]] = relationship(back_populates="parent")
    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012


class Tag(Base):
    __tablename__ = "write_tags"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(50), unique=True)
    version: Mapped[int] = mapped_column(default=1)
    parents: Mapped[list[Parent]] = relationship(secondary=links, back_populates="tags")
    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012


class Child(Base):
    __tablename__ = "write_children"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(50))
    parent_id: Mapped[int | None] = mapped_column(sa.ForeignKey(Parent.id))
    parent: Mapped[Parent | None] = relationship(back_populates="children")
    version: Mapped[int] = mapped_column(default=1)
    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012


class ParentBuilder(ModelBuilder[Parent]):
    slug = "parents"
    fields = ("id", "name", "score", "active", "version")
    sort_fields = ("name", "score", "active", "id")

    class API(surfaces.API):
        upsert_fields: tuple[tuple[str, ...], ...] = (("name",),)
        write_relationships = ("tags", "children")

    class View(View):
        related = ("tags", "children")

    async def before_save(
        self, instance: Parent, changes: Mapping[str, Any], ctx: Context, *, created: bool
    ) -> None:
        if instance.score == -1:
            raise PermissionDenied("Rejected by hook.")

    async def after_commit(self, event: DomainEvent, ctx: Context) -> None:
        self.app.app.state.events.append(event.action)


STAFF = Principal(id=1, is_authenticated=True, is_staff=True)


class TagBuilder(ModelBuilder[Tag]):
    fields = ("name", "version")


class ChildBuilder(ModelBuilder[Child]):
    slug = "children"
    fields = ("name", "parent_id", "version")


@asynccontextmanager
async def application(
    path: Path,
    *,
    configured: type[ModelBuilder[Parent]] = ParentBuilder,
    policy: StaffOnly[Any] | None = None,
    idempotency: bool = True,
    extra_builders: tuple[type[ModelBuilder[Any]], ...] = (),
) -> AsyncIterator[Obox]:
    engine = create_async_engine(os.getenv("OBOX_TEST_DATABASE_URL", f"sqlite+aiosqlite:///{path}"))
    namespace = f"writes_{uuid.uuid4().hex}" if engine.dialect.name == "postgresql" else None
    if namespace:
        async with engine.begin() as conn:
            await conn.execute(sa.schema.CreateSchema(namespace))
        engine = engine.execution_options(schema_translate_map={None: namespace})
    async with engine.begin() as conn:
        for metadata in (Base.metadata, ReceiptBase.metadata, ActivityBase.metadata):
            await conn.run_sync(metadata.create_all)
    app = Obox(
        FastAPI(),
        settings=Settings(
            session_factory=async_sessionmaker(engine, expire_on_commit=False),
            principal_dependency=lambda: STAFF,
            default_policy=policy or StaffOnly(),
            csrf_secret="s" * 32,
        ),
        plugins=[Activity(), *([Idempotency()] if idempotency else [])],
    )
    app.app.state.events = []
    try:
        app.register(configured)
        app.register(TagBuilder)
        app.register(ChildBuilder)
        for extra in extra_builders:
            app.register(extra)
        async with app.app.router.lifespan_context(app.app):
            yield app
    finally:
        if namespace:
            async with engine.begin() as conn:
                await conn.execute(sa.schema.DropSchema(namespace, cascade=True))
        await engine.dispose()


async def request(app: Obox, method: str, path: str, **kwargs: Any) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app.app), base_url="http://test"
    ) as client:
        return await client.request(method, path, **kwargs)


async def upsert(app: Obox, name: str, **data: Any) -> httpx.Response:
    token = data.pop("ifMatch", None)
    return await request(
        app,
        "POST",
        "/api/parents:batchWrite",
        json={
            "operations": [
                {
                    "op": "upsert",
                    "mergeOn": ["name"],
                    "data": {"name": name, **data},
                    **({"ifMatch": token} if token is not None else {}),
                }
            ]
        },
    )


@pytest.mark.parametrize(
    "order",
    [
        "score.asc",
        "score.desc",
        "score.asc.nullsfirst",
        "score.desc.nullslast",
        "active.asc.nullsfirst,score.desc.nullslast,name.asc",
        "score.desc.nullsfirst,active.desc.nullslast,id.desc",
    ],
)
@pytest.mark.parametrize("limit", [1, 3])
def test_nullable_cursor_matches_complete_order(path: Path, order: str, limit: int) -> None:
    async def scenario() -> None:
        async with application(path) as app:
            async with app.uow(principal=STAFF) as ctx:
                ctx.session.add_all(
                    Parent(name=str(i), score=score, active=active)
                    for i, (score, active) in enumerate(
                        [
                            (None, None),
                            (2, True),
                            (None, False),
                            (1, None),
                            (2, False),
                            (1, True),
                            (None, None),
                        ]
                    )
                )
            expected = (await request(app, "GET", "/api/parents", params={"order": order})).json()
            seen, cursor = [], None
            for _ in range(10):
                response = await request(
                    app,
                    "GET",
                    "/api/parents",
                    params={
                        "order": order,
                        "limit": limit,
                        "count": "exact",
                        **({"cursor": cursor} if cursor else {}),
                    },
                )
                assert response.status_code == 200, response.text
                body = response.json()
                assert body["page"]["total"] == 7
                seen.extend(body["items"])
                cursor = body["page"]["nextCursor"]
                if not body["page"]["hasMore"]:
                    assert cursor is None
                    break
                assert cursor is not None
            assert seen == expected["items"]

    asyncio.run(scenario())


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "writes.sqlite3"


@pytest.mark.parametrize("idempotency", [False, True])
@pytest.mark.parametrize("upsert_enabled", [False, True])
def test_openapi_exposes_configured_write_contracts(
    path: Path, idempotency: bool, upsert_enabled: bool
) -> None:
    class Configured(ParentBuilder):
        class API(ParentBuilder.API):
            upsert_fields = ParentBuilder.API.upsert_fields if upsert_enabled else ()

    async def scenario() -> None:
        async with application(path, configured=Configured, idempotency=idempotency) as app:
            response = await request(app, "GET", "/openapi.json")
            assert response.status_code == 200
            document = response.json()
            paths, schemas = document["paths"], document["components"]["schemas"]
            for url in ("/api/parents", "/api/parents:batchWrite"):
                headers = {
                    item["name"]
                    for item in paths[url]["post"].get("parameters", [])
                    if item["in"] == "header"
                }
                assert ("Idempotency-Key" in headers) is idempotency
            batch = paths["/api/parents:batchWrite"]["post"]
            body = schemas[
                batch["requestBody"]["content"]["application/json"]["schema"]["$ref"].rsplit(
                    "/", 1
                )[-1]
            ]
            operations = body["properties"]["operations"]["items"]["discriminator"]["mapping"]
            assert set(operations) == {"create", "update", "delete"} | (
                {"upsert"} if upsert_enabled else set()
            )
            if upsert_enabled:
                operation = schemas[operations["upsert"].rsplit("/", 1)[-1]]
                assert {"op", "mergeOn", "data"} <= set(operation["required"])
                assert "ifMatch" in operation["properties"]
                assert "id" not in operation["properties"]
            relation = paths["/api/parents/{id}/relationships/{field}"]
            assert {"get", "post", "put", "delete"} <= relation.keys()
            for method in ("post", "put", "delete"):
                assert any(
                    item["name"] == "If-Match" and item["required"]
                    for item in relation[method]["parameters"]
                )
                reference = relation[method]["requestBody"]["content"]["application/json"][
                    "schema"
                ]["$ref"]
                properties = schemas[reference.rsplit("/", 1)[-1]]["properties"]
                assert {"recordIds", "ifMatches"} <= properties.keys()
            result = await upsert(app, "documented")
            assert result.status_code == (200 if upsert_enabled else 422), result.text
            if upsert_enabled:
                assert result.json()["results"][0]["created"] is True

    asyncio.run(scenario())


def test_upsert_preserves_versions_hooks_activity_and_batch_rollback(path: Path) -> None:
    async def scenario() -> None:
        async with application(path) as app:
            created = await upsert(app, "one", score=1)
            assert created.status_code == 200, created.text
            result = created.json()["results"][0]
            assert result["created"] is True
            assert (await upsert(app, "one", score=2)).status_code == 428
            updated = await upsert(app, "one", score=2, ifMatch=result["etag"])
            assert updated.status_code == 200, updated.text
            next_result = updated.json()["results"][0]
            assert next_result["created"] is False
            assert next_result["id"] == result["id"]
            assert next_result["record"]["score"] == 2
            assert (await upsert(app, "one", ifMatch=result["etag"])).status_code == 412
            assert (await upsert(app, "two", score=-1)).status_code == 403
            rejected = await request(
                app,
                "POST",
                "/api/parents:batchWrite",
                json={
                    "operations": [
                        {"op": "upsert", "mergeOn": ["name"], "data": {"name": "new"}},
                        {"op": "create", "data": {"name": "bad", "score": -1}},
                    ]
                },
            )
            assert rejected.status_code == 403
            assert rejected.json()["errors"][0]["pointer"].startswith("#/operations/1")
            body = (await request(app, "GET", "/api/parents")).json()
            assert [row["name"] for row in body["items"]] == ["one"]
            assert app.app.state.events == ["create", "update"]
            async with app.uow(principal=STAFF) as ctx:
                events = list(
                    (await ctx.session.scalars(sa.select(Event).order_by(Event.id))).all()
                )
                assert [event.action for event in events] == ["create", "update"]
                assert any(change["field"] == "score" for change in events[-1].changes or [])

    asyncio.run(scenario())


def test_concurrent_upserts_resolve_unique_insert_race_without_duplicate_rows(path: Path) -> None:
    async def scenario() -> None:
        barrier = asyncio.Barrier(2)

        class Racing(ParentBuilder):
            async def before_save(
                self, instance: Parent, changes: Mapping[str, Any], ctx: Context, *, created: bool
            ) -> None:
                if created:
                    await asyncio.wait_for(barrier.wait(), timeout=10)
                await super().before_save(instance, changes, ctx, created=created)

        async with application(path, configured=Racing) as app:
            responses = await asyncio.gather(
                *(upsert(app, "race", score=score, ifMatch="*") for score in (1, 2))
            )
            assert [response.status_code for response in responses] == [200, 200], [
                response.text for response in responses
            ]
            results = [response.json()["results"][0] for response in responses]
            assert sorted(result["created"] for result in results) == [False, True]
            assert results[0]["id"] == results[1]["id"]
            assert app.app.state.events == ["create", "update"]
            rows = (await request(app, "GET", "/api/parents")).json()["items"]
            assert len(rows) == 1
            assert rows[0]["version"] == 2

    asyncio.run(scenario())


def test_upsert_does_not_update_hidden_records_or_bypass_policy_check(path: Path) -> None:
    class Scoped(StaffOnly[Any]):
        def scope(self, action: str, ctx: Context) -> Any:
            return Parent.name != "hidden"

        async def check(self, action: str, instance: Any, ctx: Context) -> bool:
            return bool(instance.score != 99)

    async def scenario() -> None:
        async with application(path, policy=Scoped()) as app:
            async with app.uow(principal=STAFF) as ctx:
                ctx.session.add(Parent(name="hidden", score=5))
            assert (await upsert(app, "hidden", score=8, ifMatch="*")).status_code == 409
            assert (await upsert(app, "rejected", score=99)).status_code == 403
            async with app.uow(principal=STAFF) as ctx:
                rows = (await ctx.session.scalars(sa.select(Parent))).all()
                assert len(rows) == 1
                assert rows[0].score == 5

    asyncio.run(scenario())


def test_upsert_rejects_unconfigured_keys_duplicates_and_disabled_operations(path: Path) -> None:
    class CreateOnly(ParentBuilder):
        class API(ParentBuilder.API):
            operations = ("list", "retrieve", "create")

    async def scenario() -> None:
        async with application(path) as app:
            for operations in [
                [{"op": "upsert", "mergeOn": ["score"], "data": {"name": "one", "score": 1}}],
                [{"op": "upsert", "mergeOn": ["name", "name"], "data": {"name": "one"}}],
                [{"op": "upsert", "mergeOn": ["name"], "data": {}}],
                [{"op": "upsert", "mergeOn": ["name"], "data": {"name": "one"}}] * 2,
            ]:
                response = await request(
                    app, "POST", "/api/parents:batchWrite", json={"operations": operations}
                )
                assert response.status_code == 422, response.text
            assert (await request(app, "GET", "/api/parents")).json()["items"] == []
        async with application(path.parent / "disabled.sqlite3", configured=CreateOnly) as app:
            assert (await upsert(app, "one")).status_code == 403

    asyncio.run(scenario())


def test_idempotency_replays_success_and_rejects_body_mismatch(path: Path) -> None:
    async def scenario() -> None:
        async with application(path) as app:
            headers = {"Idempotency-Key": "create-1"}
            first = await request(
                app, "POST", "/api/parents", json={"name": "one"}, headers=headers
            )
            second = await request(
                app, "POST", "/api/parents", json={"name": "one"}, headers=headers
            )
            assert first.status_code == second.status_code == 201
            assert first.json() == second.json()
            assert first.headers["etag"] == second.headers["etag"]
            assert first.headers["location"] == second.headers["location"]
            assert app.app.state.events == ["create"]
            assert (
                await request(app, "POST", "/api/parents", json={"name": "two"}, headers=headers)
            ).status_code == 409
            failed_headers = {"Idempotency-Key": "retry-failure"}
            assert (
                await request(
                    app,
                    "POST",
                    "/api/parents",
                    json={"name": "bad", "score": -1},
                    headers=failed_headers,
                )
            ).status_code == 403
            assert (
                await request(
                    app, "POST", "/api/parents", json={"name": "good"}, headers=failed_headers
                )
            ).status_code == 201
            async with app.uow(principal=STAFF) as ctx:
                assert (
                    await ctx.session.scalar(sa.select(sa.func.count()).select_from(Receipt)) == 2
                )

    asyncio.run(scenario())


def test_idempotency_batch_replays_stale_conditions_and_concurrent_requests(path: Path) -> None:
    async def scenario() -> None:
        async with application(path) as app:
            created = await request(app, "POST", "/api/parents", json={"name": "one"})
            body = {
                "operations": [
                    {
                        "op": "update",
                        "id": created.json()["id"],
                        "ifMatch": created.headers["etag"],
                        "data": {"score": 4},
                    }
                ]
            }
            responses = await asyncio.gather(
                *(
                    request(
                        app,
                        "POST",
                        "/api/parents:batchWrite",
                        json=body,
                        headers={"Idempotency-Key": "batch-key"},
                    )
                    for _ in range(2)
                )
            )
            assert [r.status_code for r in responses] == [200, 200], [r.text for r in responses]
            assert responses[0].json() == responses[1].json()
            assert app.app.state.events == ["create", "update"]
            creates = await asyncio.gather(
                *(
                    request(
                        app,
                        "POST",
                        "/api/parents",
                        json={"name": "two"},
                        headers={"Idempotency-Key": "parallel-create"},
                    )
                    for _ in range(2)
                )
            )
            assert [r.status_code for r in creates] == [201, 201], [r.text for r in creates]
            assert creates[0].json() == creates[1].json()

    asyncio.run(scenario())


def test_idempotency_expiry_validation_and_permissions(path: Path) -> None:
    class Policy(StaffOnly[Any]):
        restricted = False

        def read_fields(self, ctx: Context, *, obj: Any = None) -> frozenset[str] | None:
            return frozenset({"id"}) if self.restricted else None

    async def scenario() -> None:
        policy = Policy()
        async with application(path, policy=policy) as app:
            for key in ("", "contains spaces", "x" * 256):
                response = await request(
                    app,
                    "POST",
                    "/api/parents",
                    json={"name": "one"},
                    headers={"Idempotency-Key": key},
                )
                assert response.status_code == 422
            headers = {"Idempotency-Key": "expiry"}
            first = await request(
                app, "POST", "/api/parents", json={"name": "one"}, headers=headers
            )
            assert first.status_code == 201
            policy.restricted = True
            replay = await request(
                app, "POST", "/api/parents", json={"name": "one"}, headers=headers
            )
            assert replay.status_code == 403
            policy.restricted = False
            async with app.uow(principal=STAFF) as ctx:
                await ctx.session.execute(
                    sa.update(Receipt).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
                )
            second = await request(
                app, "POST", "/api/parents", json={"name": "two"}, headers=headers
            )
            assert second.status_code == 201
            assert first.json()["id"] != second.json()["id"]
        async with application(path.parent / "disabled.sqlite3", idempotency=False) as app:
            response = await request(
                app,
                "POST",
                "/api/parents",
                json={"name": "one"},
                headers={"Idempotency-Key": "disabled"},
            )
            assert response.status_code == 422

    asyncio.run(scenario())


def test_idempotency_identity_endpoint_and_create_only_scope(path: Path) -> None:
    async def scenario() -> None:
        async with application(path) as app:
            headers = {"Idempotency-Key": "shared-key"}
            body = {"name": "one"}
            dependency = app.settings.principal_dependency
            assert dependency is not None
            permission = app.get_builder(Parent).permission_name("create")
            app.app.dependency_overrides[dependency] = lambda: ScopedIdentity(
                STAFF, frozenset({permission})
            )
            first = await request(app, "POST", "/api/parents", json=body, headers=headers)
            second = await request(app, "POST", "/api/parents", json=body, headers=headers)
            assert first.status_code == second.status_code == 201
            assert first.json() == second.json()
            app.app.dependency_overrides[dependency] = lambda: Principal(
                id=2, is_authenticated=True, is_staff=True
            )
            other = await request(
                app, "POST", "/api/parents", json={"name": "two"}, headers=headers
            )
            assert other.status_code == 201
            app.app.dependency_overrides.clear()
            tag = await request(app, "POST", "/api/tags", json=body, headers=headers)
            assert tag.status_code == 201
            async with app.uow(principal=STAFF) as ctx:
                assert (
                    await ctx.session.scalar(sa.select(sa.func.count()).select_from(Receipt)) == 3
                )

    asyncio.run(scenario())


@pytest.mark.parametrize(("field", "target"), [("tags", "tags"), ("children", "children")])
def test_relationship_add_replace_remove_preserves_rows_and_versions(
    path: Path,
    field: str,
    target: str,
) -> None:
    async def scenario() -> None:
        async with application(path) as app:
            parent = await request(app, "POST", "/api/parents", json={"name": "one"})
            children = [
                await request(app, "POST", f"/api/{target}", json={"name": name})
                for name in ("a", "b")
            ]
            assert all(child.status_code == 201 for child in children)
            parent_id = parent.json()["id"]
            url = f"/api/parents/{parent_id}/relationships/{field}"
            assert (await request(app, "POST", url, json={"recordIds": []})).status_code == 428
            added = await request(
                app,
                "POST",
                url,
                json={
                    "recordIds": [children[0].json()["id"]],
                    "ifMatches": {children[0].json()["id"]: children[0].headers["etag"]},
                },
                headers={"If-Match": parent.headers["etag"]},
            )
            assert added.status_code == 200, added.text
            assert added.headers["etag"] != parent.headers["etag"]
            related = await request(app, "GET", url)
            assert related.status_code == 200
            assert [item["id"] for item in related.json()["items"]] == [children[0].json()["id"]]
            history = await request(app, "GET", f"/api/parents/{parent_id}/activity?kind=revision")
            assert history.status_code == 200, history.text
            assert history.json()["items"][0]["changes"] == [
                {
                    "field": field,
                    "kind": "reference",
                    "before": [],
                    "after": [children[0].json()["id"]],
                }
            ]
            a = await request(app, "GET", f"/api/{target}/{children[0].json()['id']}")
            b = children[1]
            replaced = await request(
                app,
                "PUT",
                url,
                json={
                    "recordIds": [b.json()["id"]],
                    "ifMatches": {
                        a.json()["id"]: a.headers["etag"],
                        b.json()["id"]: b.headers["etag"],
                    },
                },
                headers={"If-Match": added.headers["etag"]},
            )
            assert replaced.status_code == 200, replaced.text
            b = await request(app, "GET", f"/api/{target}/{b.json()['id']}")
            removed = await request(
                app,
                "DELETE",
                url,
                json={
                    "recordIds": [b.json()["id"]],
                    "ifMatches": {b.json()["id"]: b.headers["etag"]},
                },
                headers={"If-Match": replaced.headers["etag"]},
            )
            assert removed.status_code == 200, removed.text
            async with app.uow(principal=STAFF) as ctx:
                obj = await ctx.session.get(Parent, int(parent_id))
                assert obj is not None
                await ctx.session.refresh(obj, attribute_names=[field])
                assert getattr(obj, field) == []
                assert (
                    await ctx.session.scalar(
                        sa.select(sa.func.count()).select_from(Tag if target == "tags" else Child)
                    )
                    == 2
                )

    asyncio.run(scenario())


def test_relationship_rejects_stale_targets_scope_and_reparenting(path: Path) -> None:
    class Policy(StaffOnly[Any]):
        denied = False

        async def allow(self, action: str, ctx: Context, *, obj: Any = None) -> bool:
            return not (self.denied and isinstance(obj, Tag)) and await super().allow(
                action, ctx, obj=obj
            )

    async def scenario() -> None:
        policy = Policy()
        async with application(path, policy=policy) as app:
            parent = await request(app, "POST", "/api/parents", json={"name": "one"})
            tag = await request(app, "POST", "/api/tags", json={"name": "tag"})
            url = f"/api/parents/{parent.json()['id']}/relationships/tags"
            headers = {"If-Match": parent.headers["etag"]}
            for body, status in [
                ({"recordIds": [tag.json()["id"]]}, 428),
                (
                    {"recordIds": [tag.json()["id"]], "ifMatches": {tag.json()["id"]: '"stale"'}},
                    412,
                ),
                ({"recordIds": ["99999"]}, 404),
                ({"recordIds": [tag.json()["id"]] * 2}, 422),
            ]:
                response = await request(app, "POST", url, json=body, headers=headers)
                assert response.status_code == status, response.text
            policy.denied = True
            response = await request(
                app,
                "POST",
                url,
                json={
                    "recordIds": [tag.json()["id"]],
                    "ifMatches": {tag.json()["id"]: tag.headers["etag"]},
                },
                headers=headers,
            )
            assert response.status_code == 403
            policy.denied = False
            other = await request(app, "POST", "/api/parents", json={"name": "other"})
            child = await request(
                app,
                "POST",
                "/api/children",
                json={
                    "name": "child",
                    "parentId": other.json()["id"],
                },
            )
            response = await request(
                app,
                "POST",
                url.removesuffix("tags") + "children",
                json={
                    "recordIds": [child.json()["id"]],
                    "ifMatches": {child.json()["id"]: child.headers["etag"]},
                },
                headers=headers,
            )
            assert response.status_code == 409
            async with app.uow(principal=STAFF) as ctx:
                assert await ctx.session.scalar(sa.select(sa.func.count()).select_from(links)) == 0
            added = await request(
                app,
                "POST",
                url,
                json={
                    "recordIds": [tag.json()["id"]],
                    "ifMatches": {tag.json()["id"]: tag.headers["etag"]},
                },
                headers=headers,
            )
            assert added.status_code == 200, added.text
            policy.denied = True
            history = await request(app, "GET", f"/api/parents/{parent.json()['id']}/activity")
            assert history.status_code == 200, history.text
            assert history.json()["items"][0]["changes"][0]["after"] == [None]

    asyncio.run(scenario())


@pytest.mark.parametrize("keys", [(("score",),), (("score", "name"),), (("name", "name"),)])
def test_upsert_configuration_requires_exact_unique_key(
    path: Path, keys: tuple[tuple[str, ...]]
) -> None:
    class Invalid(ParentBuilder):
        class API(ParentBuilder.API):
            upsert_fields = keys

    async def scenario() -> None:
        with pytest.raises(ImproperlyConfigured, match="UNIQUE"):
            async with application(path, configured=Invalid):
                pass

    asyncio.run(scenario())


def test_upsert_composite_key_presets_and_column_aliases(path: Path) -> None:
    class Compound(Base):
        __tablename__ = "write_compound"
        __table_args__ = (
            sa.UniqueConstraint("account_key", "external_code"),
            sa.Index(
                "uq_compound_partial",
                "title",
                unique=True,
                sqlite_where=sa.text("id < 0"),
                postgresql_where=sa.text("id < 0"),
            ),
        )
        id: Mapped[int] = mapped_column(primary_key=True)
        account_id: Mapped[str] = mapped_column("account_key", sa.String(50))
        external_key: Mapped[str] = mapped_column("external_code", sa.String(50))
        title: Mapped[str] = mapped_column(sa.String(50))
        optional_key: Mapped[str | None] = mapped_column(sa.String(50), unique=True)

    class AccountPolicy(StaffOnly[Compound]):
        def presets(self, action: str, ctx: Context) -> Mapping[str, Any]:
            return {"account_id": "account-one"}

    class CompoundBuilder(ModelBuilder[Compound]):
        slug = "compound"
        fields = ("id", "account_id", "external_key", "title", "optional_key")
        policy = AccountPolicy()

        class API(surfaces.API):
            upsert_fields: tuple[tuple[str, ...], ...] = (("account_id", "external_key"),)

    async def scenario() -> None:
        for key in ("title", "optional_key"):

            class Invalid(CompoundBuilder):
                class API(CompoundBuilder.API):
                    upsert_fields = ((key,),)

            app = Obox(
                FastAPI(),
                settings=Settings(
                    database_url="sqlite+aiosqlite://",
                    default_policy=StaffOnly(),
                    principal_dependency=lambda: STAFF,
                ),
            )
            with pytest.raises(ImproperlyConfigured, match="UNIQUE"):
                app.register(Invalid)
        async with application(path, extra_builders=(CompoundBuilder,)) as app:

            async def write(data: dict[str, Any], names: list[str] | None = None) -> httpx.Response:
                return await request(
                    app,
                    "POST",
                    "/api/compound:batchWrite",
                    json={
                        "operations": [
                            {
                                "op": "upsert",
                                "mergeOn": names or ["accountId", "externalKey"],
                                "data": data,
                            }
                        ]
                    },
                )

            assert (await write({"externalKey": "x"})).status_code == 422
            first = await write({"externalKey": "x", "title": "initial"})
            assert first.status_code == 200, first.text
            assert first.json()["results"][0]["created"] is True
            async with app.uow(principal=STAFF) as ctx:
                ctx.session.add(
                    Compound(account_id="account-two", external_key="x", title="foreign")
                )
            second = await write({"externalKey": "x", "title": "changed"})
            assert second.status_code == 200, second.text
            assert second.json()["results"][0]["created"] is False
            assert second.json()["results"][0]["id"] == first.json()["results"][0]["id"]
            assert (
                await write({"externalKey": "x", "accountId": "account-two"})
            ).status_code == 403
            wrong_alias = await write({"externalKey": "x"}, ["account_id", "externalKey"])
            assert wrong_alias.status_code == 422
            assert wrong_alias.json()["errors"][0]["pointer"] == "#/operations/0/mergeOn"
            async with app.uow(principal=STAFF) as ctx:
                rows = (
                    await ctx.session.scalars(sa.select(Compound).order_by(Compound.account_id))
                ).all()
                assert [(row.account_id, row.title) for row in rows] == [
                    ("account-one", "changed"),
                    ("account-two", "foreign"),
                ]

    asyncio.run(scenario())
