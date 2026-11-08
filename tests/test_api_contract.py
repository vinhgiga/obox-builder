from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from obox_builder import Context, ModelBuilder, Obox, Principal, builder
from obox_builder.auth import StaffOnly
from obox_builder.builder.api_schema import ApiResponse
from obox_builder.conf import Settings
from obox_builder.contrib.activity import Activity
from obox_builder.contrib.activity.models import Base as ActivityBase
from obox_builder.contrib.activity.models import Event
from obox_builder.errors import ImproperlyConfigured, ResourceConflict
from pydantic import BaseModel, Field, create_model
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


class Category(Base):
    __tablename__ = "contract_categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str | None] = mapped_column(sa.String(50), nullable=True)


class Record(Base):
    __tablename__ = "contract_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(20))
    amount: Mapped[Decimal] = mapped_column(sa.Numeric(6, 2), default=Decimal("0"))
    moment: Mapped[datetime | None] = mapped_column(sa.DateTime(), nullable=True)
    category_id: Mapped[int | None] = mapped_column(sa.ForeignKey(Category.id), nullable=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(sa.JSON, nullable=True)
    primary_category: Mapped[Category | None] = relationship()


class Document(Base):
    __tablename__ = "contract_documents"

    document_key: Mapped[str] = mapped_column(sa.String(40), primary_key=True)
    title_text: Mapped[str] = mapped_column(sa.String(30), unique=True)
    revision: Mapped[int] = mapped_column(default=1)
    __mapper_args__ = {"version_id_col": revision}  # noqa: RUF012


class LargeRecord(Base):
    __tablename__ = "contract_large_records"

    record_key: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)


class UUIDRecord(Base):
    __tablename__ = "contract_uuid_records"

    record_key: Mapped[uuid.UUID] = mapped_column(primary_key=True)


class DocumentBuilder(ModelBuilder[Document]):
    fields = ("title_text",)
    slug = "documents"

    async def before_save(
        self, instance: Document, changes: Mapping[str, Any], ctx: Context, *, created: bool
    ) -> None:
        if created:
            instance.document_key = uuid.uuid4().hex


class CategoryBuilder(ModelBuilder[Category]):
    slug = "categories"
    label_field = "label"


class RecordBuilder(ModelBuilder[Record]):
    slug = "records"
    label_field = "name"
    filter_fields: tuple[str, ...] = ("id", "category_id", "moment", "name")
    sort_fields: tuple[str, ...] = ("id", "name", "moment", "category_id")
    permissions = ("publish",)
    actions = ("publish",)

    @builder.action(description="Publish records", permission="publish")
    def publish(self, objects: list[Record], ctx: Any) -> None:
        for obj in objects:
            obj.name = obj.name.upper()


STAFF = Principal(id=1, is_authenticated=True, is_staff=True)


@asynccontextmanager
async def application(
    configured: type[ModelBuilder[Record]] = RecordBuilder,
    principal: Principal = STAFF,
    document_builder: type[ModelBuilder[Document]] = DocumentBuilder,
    with_activity: bool = False,
) -> AsyncIterator[Obox]:
    url = os.getenv("OBOX_TEST_DATABASE_URL", "sqlite+aiosqlite://")
    engine = create_async_engine(
        url, **({"poolclass": StaticPool} if url.startswith("sqlite") else {})
    )
    namespace = (
        f"test_api_contract_{uuid.uuid4().hex}" if engine.dialect.name == "postgresql" else None
    )
    if namespace:
        async with engine.begin() as connection:
            await connection.execute(sa.schema.CreateSchema(namespace))
        engine = engine.execution_options(schema_translate_map={None: namespace})
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        if with_activity:
            await connection.run_sync(ActivityBase.metadata.create_all)
    app = Obox(
        FastAPI(),
        settings=Settings(
            session_factory=async_sessionmaker(engine, expire_on_commit=False),
            principal_dependency=lambda: principal,
            default_policy=StaffOnly(),
            csrf_secret="x" * 32,
        ),
        plugins=[Activity()] if with_activity else [],
    )
    try:
        app.register(CategoryBuilder)
        app.register(configured)
        app.register(document_builder)
        app.register(LargeRecord)
        app.register(UUIDRecord)
        async with app.app.router.lifespan_context(app.app):
            yield app
    finally:
        if namespace:
            async with engine.begin() as connection:
                await connection.execute(sa.schema.DropSchema(namespace, cascade=True))
        await engine.dispose()


async def request(app: Obox, method: str, path: str, **kwargs: Any) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app.app), base_url="http://test"
    ) as client:
        return await client.request(method, path, **kwargs)


def test_api_rejects_values_that_database_would_truncate_or_fail() -> None:
    async def scenario() -> None:
        async with application() as app:
            decimal_value = await request(
                app,
                "POST",
                "/api/records",
                json={"name": "decimal", "amount": "12.345"},
            )
            assert decimal_value.status_code == 422
            assert decimal_value.json()["errors"][0]["code"] == "invalid_type"

            nul_value = await request(app, "POST", "/api/records", json={"name": "bad\x00value"})
            assert nul_value.status_code == 422

            huge_filter = await request(
                app, "GET", "/api/records?id=eq.999999999999999999999999999"
            )
            assert huge_filter.status_code == 422

    asyncio.run(scenario())


def test_api_normalizes_datetime_and_returns_database_numeric_value() -> None:
    async def scenario() -> None:
        async with application() as app:
            created = await request(
                app,
                "POST",
                "/api/records",
                json={
                    "name": "time",
                    "amount": "12.34",
                    "moment": "2026-11-05T09:00:00+07:00",
                },
            )
            assert created.status_code == 201
            assert created.json()["amount"] == "12.34"
            assert created.json()["moment"] == "2026-11-05T02:00:00Z"

    asyncio.run(scenario())


def test_api_expansion_and_http_error_contract() -> None:
    async def scenario() -> None:
        async with application() as app:
            async with app.uow(principal=STAFF) as ctx:
                ctx.session.add_all([Category(label=None), Record(name="plain")])

            expanded = await request(app, "GET", "/api/records?expand=primaryCategory")
            assert expanded.status_code == 200
            assert expanded.json()["items"][0]["expanded"]["primaryCategory"] is None

            not_acceptable = await request(
                app, "GET", "/api/records", headers={"accept": "text/csv"}
            )
            assert not_acceptable.status_code == 406
            assert not_acceptable.headers["content-type"] == "application/problem+json"
            assert not_acceptable.json()["code"] == "not_acceptable"

            missing = await request(app, "GET", "/api/nope")
            assert missing.status_code == 404
            assert missing.headers["content-type"] == "application/problem+json"

            method = await request(app, "POST", "/api/records/1", json={})
            assert method.status_code == 405
            assert method.json()["code"] == "method_not_allowed"

            action_type = await request(
                app,
                "POST",
                "/api/records:publish",
                content="{}",
                headers={"content-type": "text/plain"},
            )
            assert action_type.status_code == 415
            assert action_type.headers["cache-control"] == "private, no-store"

    asyncio.run(scenario())


def test_api_openapi_declares_stable_operations_and_query_parameters() -> None:
    async def scenario() -> None:
        async with application() as app:
            schema = app.app.openapi()
            list_operation = schema["paths"]["/api/records"]["get"]
            parameters = {parameter["name"] for parameter in list_operation["parameters"]}
            assert {
                "select",
                "expand",
                "order",
                "limit",
                "offset",
                "cursor",
                "count",
                "id",
            } <= parameters
            assert list_operation["operationId"] == "records_list"
            assert schema["paths"]["/api/records/{id}"]["get"]["operationId"] == "records_retrieve"
            assert "Problem" in schema["components"]["schemas"]
            assert "RecordListItem" in schema["components"]["schemas"]
            assert "RecordResponse" in schema["components"]["schemas"]
            assert not any(
                name.startswith("obox_builder__") for name in schema["components"]["schemas"]
            )
            assert schema["components"]["schemas"]["RecordResponse"]["properties"]["amount"] == {
                "title": "Amount",
                "type": "string",
            }
            for name in ("RecordListItem", "RecordResponse"):
                moment = schema["components"]["schemas"][name]["properties"]["moment"]
                assert {"type": "string", "format": "date-time"} in moment["anyOf"]
            assert schema["components"]["schemas"]["Page"]["properties"]["offset"]["minimum"] == 0
            assert (
                schema["components"]["schemas"]["Problem"]["properties"]["traceId"]["pattern"]
                == r"^[A-Za-z0-9._-]{1,128}$"
            )
            assert {parameter["name"] for parameter in list_operation["parameters"]} >= {
                "and",
                "or",
                "not.and",
                "not.or",
            }
            assert list_operation["responses"]["422"]["content"]["application/problem+json"][
                "schema"
            ] == {"$ref": "#/components/schemas/Problem"}

    asyncio.run(scenario())


def test_identity_is_required_even_when_omitted_from_presentation_and_selection() -> None:
    class ProjectedBuilder(RecordBuilder):
        fields = ("name",)
        filter_fields = ()
        sort_fields = ("name",)

    async def scenario() -> None:
        async with application(ProjectedBuilder) as app:
            created = await request(app, "POST", "/api/records", json={"name": "identity"})
            assert created.status_code == 201
            row = created.json()
            assert row == {"id": "1", "name": "identity"}
            assert created.headers["location"] == "/api/records/1"
            for path in ("/api/records?select=name", "/api/records?select=*"):
                listed = await request(app, "GET", path)
                assert listed.json()["items"] == [row]
            retrieved = await request(app, "GET", created.headers["location"])
            assert retrieved.json() == row
            updated = await request(app, "PATCH", "/api/records/1", json={"name": "changed"})
            assert updated.json() == {"id": "1", "name": "changed"}
            assert "id" not in app.get_builder(Record).form_fields
            schemas = app.app.openapi()["components"]["schemas"]
            for name in ("RecordListItem", "RecordResponse"):
                assert "id" in schemas[name]["required"]
                assert schemas[name]["properties"]["id"]["type"] == "string"

    asyncio.run(scenario())


def test_identifier_types_and_relationship_options_are_consistent() -> None:
    async def scenario() -> None:
        async with application() as app:
            key = uuid.uuid4()
            async with app.uow(principal=STAFF) as ctx:
                ctx.session.add_all(
                    [
                        LargeRecord(record_key=2**63 - 1),
                        UUIDRecord(record_key=key),
                        Category(id=7, label="Group"),
                    ]
                )
            for slug, expected in (("largeRecords", str(2**63 - 1)), ("uuidRecords", str(key))):
                listed = await request(app, "GET", f"/api/{slug}")
                assert listed.json()["items"] == [{"id": expected}]
                retrieved = await request(app, "GET", f"/api/{slug}/{expected}")
                assert retrieved.json() == {"id": expected}
                options = await request(app, "GET", f"/api/{slug}:queryOptions")
                assert options.json()["items"] == [{"id": expected, "label": expected}]
            created = await request(
                app,
                "POST",
                "/api/records",
                json={"name": "related", "categoryId": "7", "payload": {"keep_snake": False}},
            )
            assert created.status_code == 201
            assert created.json()["categoryId"] == "7"
            assert created.json()["payload"] == {"keep_snake": False}
            expanded = await request(app, "GET", "/api/records?expand=primaryCategory")
            assert expanded.json()["items"][0]["expanded"] == {
                "primaryCategory": {"id": "7", "label": "Group"}
            }
            options = await request(app, "GET", "/api/categories:queryOptions")
            assert options.json()["items"] == [{"id": "7", "label": "Group"}]

    asyncio.run(scenario())


def test_invalid_foreign_key_is_a_validation_error() -> None:
    async def scenario() -> None:
        async with application() as app:
            for method, path, payload in (
                ("POST", "/api/records", {"name": "orphan", "categoryId": "999"}),
                ("PATCH", "/api/records/1", {"categoryId": "999"}),
            ):
                if method == "PATCH":
                    async with app.uow(principal=STAFF) as ctx:
                        ctx.session.add(Record(name="existing"))
                response = await request(app, method, path, json=payload)
                assert response.status_code == 422
                assert response.headers["content-type"] == "application/problem+json"
                problem = response.json()
                assert problem["type"] == "urn:obox:problem:invalid_reference"
                assert problem["code"] == "invalid_reference"
                assert problem["title"] == "Unprocessable Content"
                assert problem["errors"] == [
                    {
                        "field": "categoryId",
                        "pointer": "#/categoryId",
                        "code": "invalid_reference",
                        "detail": "Related record is unavailable.",
                    }
                ]
            async with app.uow(principal=STAFF) as ctx:
                assert await ctx.session.scalar(sa.select(sa.func.count()).select_from(Record)) == 1
                assert await ctx.session.scalar(sa.select(Record.category_id)) is None

    asyncio.run(scenario())


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_public_field_names_work_in_all_query_positions(method: str) -> None:
    async def scenario() -> None:
        async with application() as app:
            async with app.uow(principal=STAFF) as ctx:
                ctx.session.add_all(
                    [
                        Category(id=7, label="Group"),
                        Record(name="first", category_id=7),
                        Record(name="second"),
                    ]
                )
            pairs = [
                ("select", "name,categoryId"),
                ("categoryId", "eq.7"),
                ("or", "(categoryId.eq.7,and(name.eq.first,categoryId.not.is.null))"),
                ("order", "categoryId.desc,id.asc"),
                ("expand", "primaryCategory"),
            ]
            response = await request(
                app,
                method,
                "/api/records" if method == "GET" else "/api/records:query",
                **({"params": pairs} if method == "GET" else {"json": {"query": pairs}}),
            )
            assert response.status_code == 200, response.text
            assert response.json()["items"] == [
                {
                    "id": "1",
                    "name": "first",
                    "categoryId": "7",
                    "expanded": {"primaryCategory": {"id": "7", "label": "Group"}},
                }
            ]
            schema = app.app.openapi()
            parameters = schema["paths"]["/api/records"]["get"]["parameters"]
            assert "categoryId" in {p["name"] for p in parameters}
            assert "category_id" not in {p["name"] for p in parameters}

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "pairs",
    [
        [("category_id", "eq.1")],
        [("select", "category_id")],
        [("order", "category_id.desc")],
        [("or", "(category_id.eq.1,name.eq.test)")],
        [("expand", "primary_category")],
    ],
)
def test_python_field_references_are_rejected(pairs: list[tuple[str, str]]) -> None:
    async def scenario() -> None:
        async with application() as app:
            for method in ("GET", "POST"):
                response = await request(
                    app,
                    method,
                    "/api/records" if method == "GET" else "/api/records:query",
                    **({"params": pairs} if method == "GET" else {"json": {"query": pairs}}),
                )
                assert response.status_code == 422

    asyncio.run(scenario())


def test_query_validation_errors_use_public_field_names() -> None:
    async def scenario() -> None:
        async with application() as app:
            response = await request(app, "GET", "/api/records?categoryId=eq.invalid")
            assert response.status_code == 422
            assert response.json()["errors"][0]["field"] == "categoryId"
            response = await request(app, "GET", "/api/records:queryOptions?categoryId=eq.invalid")
            assert response.status_code == 422
            assert response.json()["errors"][0]["field"] == "categoryId"

    asyncio.run(scenario())


@pytest.mark.parametrize("per_record", [False, True])
def test_identity_never_bypasses_field_policy(per_record: bool) -> None:
    class HiddenIdentity(StaffOnly[Record]):
        def read_fields(self, ctx: Context, *, obj: Record | None = None) -> frozenset[str] | None:
            return frozenset({"name"}) if obj is not None or not per_record else None

    class HiddenBuilder(RecordBuilder):
        fields = ("name",)
        filter_fields = ()
        sort_fields = ("name",)
        policy = HiddenIdentity()

    async def scenario() -> None:
        async with application(HiddenBuilder) as app:
            async with app.uow(principal=STAFF) as ctx:
                ctx.session.add(Record(name="protected"))
            for path in ("/api/records", "/api/records/1"):
                response = await request(app, "GET", path)
                assert response.status_code == 403
                assert "protected" not in response.text
            created = await request(app, "POST", "/api/records", json={"name": "rollback"})
            assert created.status_code == 403
            updated = await request(app, "PATCH", "/api/records/1", json={"name": "rollback"})
            assert updated.status_code == 403
            async with app.uow(principal=STAFF) as ctx:
                assert await ctx.session.scalar(sa.select(sa.func.count()).select_from(Record)) == 1
                assert await ctx.session.scalar(sa.select(Record.name)) == "protected"

    asyncio.run(scenario())


def test_versioned_records_require_and_refresh_strong_etags() -> None:
    async def scenario() -> None:
        async with application() as app:
            created = await request(app, "POST", "/api/documents", json={"titleText": "draft"})
            assert created.status_code == 201, created.text
            row, token = created.json(), created.headers["etag"]
            assert set(row) == {"id", "titleText"}
            assert isinstance(row["id"], str)
            path = created.headers["location"]
            retrieved = await request(app, "GET", path)
            assert retrieved.json() == row
            assert retrieved.headers["etag"] == token
            unchanged = await request(app, "PATCH", path, json={}, headers={"If-Match": token})
            assert unchanged.status_code == 200
            assert unchanged.headers["etag"] == token
            for method in ("PATCH", "DELETE"):
                missing = await request(
                    app, method, path, **({"json": {}} if method == "PATCH" else {})
                )
                assert missing.status_code == 428
                assert missing.json()["code"] == "precondition_required"
            changed = await request(
                app, "PATCH", path, json={"titleText": "published"}, headers={"If-Match": token}
            )
            assert changed.status_code == 200
            new_token = changed.headers["etag"]
            assert new_token != token
            for method in ("PATCH", "DELETE"):
                stale = await request(
                    app,
                    method,
                    path,
                    headers={"If-Match": token},
                    **({"json": {"titleText": "lost update"}} if method == "PATCH" else {}),
                )
                assert stale.status_code == 412
                assert stale.json()["code"] == "precondition_failed"
                assert stale.headers["content-type"] == "application/problem+json"
            current = await request(app, "GET", path)
            assert current.json()["titleText"] == "published"
            assert current.headers["etag"] == new_token
            deleted = await request(app, "DELETE", path, headers={"If-Match": new_token})
            assert deleted.status_code == 204
            assert (await request(app, "GET", path)).status_code == 404

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("header", "status"),
    [
        ("W/{etag}", 412),
        ('"wrong"', 412),
        ('"wrong", {etag}', 200),
        ("W/{etag}, {etag}", 200),
        ("*", 200),
        ("unquoted", 422),
        ("*, {etag}", 422),
        ("{etag},", 200),
        (",{etag},", 200),
        ("", 412),
    ],
)
def test_if_match_entity_tag_semantics(header: str, status: int) -> None:
    async def scenario() -> None:
        async with application() as app:
            created = await request(app, "POST", "/api/documents", json={"titleText": "draft"})
            response = await request(
                app,
                "PATCH",
                created.headers["location"],
                json={"titleText": "updated"},
                headers={"If-Match": header.format(etag=created.headers["etag"])},
            )
            assert response.status_code == status, response.text

    asyncio.run(scenario())


def test_preconditions_do_not_mask_authorization_or_constraint_errors() -> None:
    async def scenario() -> None:
        async with application(principal=Principal()) as app:
            response = await request(app, "PATCH", "/api/documents/hidden", json={})
            assert response.status_code == 401
            assert response.headers["www-authenticate"] == 'Cookie realm="obox"'
        async with application(principal=Principal(id=1, is_authenticated=True)) as app:
            response = await request(app, "PATCH", "/api/documents/hidden", json={})
            assert response.status_code == 403
        async with application() as app:
            response = await request(app, "PATCH", "/api/documents/missing", json={})
            assert response.status_code == 404
            first = await request(app, "POST", "/api/documents", json={"titleText": "first"})
            second = await request(app, "POST", "/api/documents", json={"titleText": "second"})
            wrong_record = await request(
                app,
                "PATCH",
                first.headers["location"],
                json={},
                headers={"If-Match": second.headers["etag"]},
            )
            assert wrong_record.status_code == 412
            conflict = await request(
                app,
                "PATCH",
                first.headers["location"],
                json={"titleText": "second"},
                headers={"If-Match": first.headers["etag"]},
            )
            assert conflict.status_code == 409
            current = await request(app, "GET", first.headers["location"])
            assert current.json()["titleText"] == "first"
            assert current.headers["etag"] == first.headers["etag"]
            unversioned = await request(app, "POST", "/api/records", json={"name": "plain"})
            assert "etag" not in unversioned.headers
            unsupported = await request(
                app, "PATCH", unversioned.headers["location"], json={}, headers={"If-Match": "*"}
            )
            assert unsupported.status_code == 422
            assert unsupported.json()["code"] == "unsupported_precondition"
            schema = app.app.openapi()
            operations = schema["paths"]["/api/documents/{id}"]
            for method in ("patch", "delete"):
                assert {
                    "name": "If-Match",
                    "in": "header",
                    "required": True,
                    "schema": {"type": "string"},
                } in operations[method]["parameters"]
                assert {"412", "428"} <= operations[method]["responses"].keys()
            assert "ETag" in operations["get"]["responses"]["200"]["headers"]
            unversioned_operations = schema["paths"]["/api/records/{id}"]
            for method in ("patch", "delete"):
                assert "If-Match" not in {
                    parameter["name"]
                    for parameter in unversioned_operations[method].get("parameters", [])
                }

    asyncio.run(scenario())


@pytest.mark.parametrize("hidden_by", ["permission", "scope"])
def test_versioned_writes_use_mutation_permissions_and_scope(hidden_by: str) -> None:
    class MutationPolicy(StaffOnly[Document]):
        async def allow(self, action: str, ctx: Context, *, obj: Document | None = None) -> bool:
            if action == "retrieve" and hidden_by == "permission":
                return False
            return await super().allow(action, ctx, obj=obj)

        def scope(self, action: str, ctx: Context) -> sa.ColumnElement[bool]:
            if action == "retrieve" and hidden_by == "scope":
                return sa.false()
            return super().scope(action, ctx)

    class MutationBuilder(DocumentBuilder):
        policy = MutationPolicy()

    async def scenario() -> None:
        async with application(document_builder=MutationBuilder) as app:
            created = await request(app, "POST", "/api/documents", json={"titleText": "draft"})
            assert created.status_code == 201
            path, tag = created.headers["location"], created.headers["etag"]
            hidden = await request(app, "GET", path)
            assert hidden.status_code == (403 if hidden_by == "permission" else 404)
            missing = await request(app, "PATCH", path, json={})
            assert missing.status_code == 428
            updated = await request(
                app, "PATCH", path, json={"titleText": "published"}, headers={"If-Match": tag}
            )
            assert updated.status_code == 200, updated.text
            assert updated.json()["titleText"] == "published"
            stale = await request(app, "DELETE", path, headers={"If-Match": tag})
            assert stale.status_code == 412
            deleted = await request(
                app, "DELETE", path, headers={"If-Match": updated.headers["etag"]}
            )
            assert deleted.status_code == 204

    asyncio.run(scenario())


def test_service_version_checks_remain_available_without_http() -> None:
    async def scenario() -> None:
        async with application() as app:
            async with app.uow(principal=STAFF) as ctx:
                obj = await app.service(Document).create({"title_text": "draft"}, ctx=ctx)
                key, version = obj.document_key, obj.revision
            async with app.uow(principal=STAFF) as ctx:
                changed = await app.service(Document).update(
                    key, {"title_text": "updated"}, version=version, ctx=ctx
                )
                assert changed.revision == version + 1

            async def stale_write(action: str) -> None:
                async with app.uow(principal=STAFF) as ctx:
                    if action == "update":
                        await app.service(Document).update(key, {}, version=version, ctx=ctx)
                    else:
                        await app.service(Document).delete(key, version=version, ctx=ctx)

            for action in ("update", "delete"):
                with pytest.raises(ResourceConflict):
                    await stale_write(action)

    asyncio.run(scenario())


@pytest.mark.skipif(
    not os.getenv("OBOX_TEST_DATABASE_URL", "").startswith("postgresql"),
    reason="Requires an isolated PostgreSQL schema",
)
def test_two_writers_cannot_commit_the_same_record_version() -> None:
    async def scenario() -> None:
        async with application() as app:
            created = await request(app, "POST", "/api/documents", json={"titleText": "draft"})
            results = await asyncio.gather(
                *(
                    request(
                        app,
                        "PATCH",
                        created.headers["location"],
                        json={"titleText": title},
                        headers={"If-Match": created.headers["etag"]},
                    )
                    for title in ("writer one", "writer two")
                )
            )
            assert sorted(response.status_code for response in results) == [200, 412]
            winner = next(response for response in results if response.status_code == 200)
            current = await request(app, "GET", created.headers["location"])
            assert current.json() == winner.json()
            assert current.headers["etag"] == winner.headers["etag"]

    asyncio.run(scenario())


def test_activity_uses_public_field_references_after_redaction() -> None:
    class ProjectedBuilder(RecordBuilder):
        class API(builder.API):
            read_fields = ("category_id", "payload")

    async def scenario() -> None:
        async with application(ProjectedBuilder, with_activity=True) as app:
            async with app.uow(principal=STAFF) as ctx:
                ctx.session.add_all(
                    [
                        Category(id=7, label="Group"),
                        Record(id=1, name="protected", category_id=7),
                        Event(
                            resource_key=Record.__tablename__,
                            record_id="1",
                            kind="revision",
                            action="update",
                            source="api",
                            changes=[
                                {"field": "category_id", "kind": "set", "before": None, "after": 7},
                                {"field": "payload", "kind": "set", "after": {"keep_snake": False}},
                                {"field": "name", "kind": "set", "after": "protected"},
                            ],
                        ),
                    ]
                )
            response = await request(app, "GET", "/api/records/1/activity")
            assert response.status_code == 200, response.text
            assert response.json()["items"][0]["changes"] == [
                {"field": "categoryId", "kind": "set", "before": None, "after": "7"},
                {"field": "payload", "kind": "set", "after": {"keep_snake": False}},
            ]
            assert response.json()["items"][0]["occurredAt"].endswith("Z")
            assert "protected" not in response.text
            schemas = app.app.openapi()["components"]["schemas"]
            assert schemas["RevisionItem"]["properties"]["occurredAt"]["format"] == "date-time"
            assert schemas["CommentRead"]["properties"]["createdAt"]["format"] == "date-time"
            assert (
                schemas["CommentRead"]["properties"]["editedAt"]["anyOf"][0]["format"]
                == "date-time"
            )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("kind", "message"),
    [
        ("missing", "must declare the primary key"),
        ("alias", "public field names"),
        ("collision", "unique camelCase"),
    ],
)
def test_inconsistent_custom_api_schemas_fail_at_registration(kind: str, message: str) -> None:
    class InvalidAPI(builder.API):
        def get_read_schema(self, *, builder: Any) -> type[BaseModel]:
            if kind == "missing":
                return create_model("MissingIdentity", __base__=ApiResponse)
            return create_model(
                "InvalidAlias",
                __base__=super().get_read_schema(builder=builder),
                title_text=(str, Field(alias="id" if kind == "collision" else "otherTitle")),
            )

    class InvalidBuilder(DocumentBuilder):
        API = InvalidAPI

    async def start() -> None:
        async with application(document_builder=InvalidBuilder):
            pass

    with pytest.raises(ImproperlyConfigured, match=message):
        asyncio.run(start())
