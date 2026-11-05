from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx
import sqlalchemy as sa
from fastapi import FastAPI
from obox_builder import ModelBuilder, Obox, Principal, builder
from obox_builder.auth import StaffOnly
from obox_builder.conf import Settings
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
    category: Mapped[Category | None] = relationship()


class CategoryBuilder(ModelBuilder[Category]):
    slug = "categories"
    label_field = "label"


class RecordBuilder(ModelBuilder[Record]):
    slug = "records"
    label_field = "name"
    filter_fields = ("id",)
    sort_fields = ("id", "name")
    permissions = ("publish",)
    actions = ("publish",)

    @builder.action(description="Publish records", permission="publish")
    def publish(self, objects: list[Record], ctx: Any) -> None:
        for obj in objects:
            obj.name = obj.name.upper()


STAFF = Principal(id=1, is_authenticated=True, is_staff=True)


@asynccontextmanager
async def application() -> AsyncIterator[Obox]:
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
    app = Obox(
        FastAPI(),
        settings=Settings(
            session_factory=async_sessionmaker(engine, expire_on_commit=False),
            principal_dependency=lambda: STAFF,
            default_policy=StaffOnly(),
            csrf_secret="x" * 32,
        ),
    )
    app.register(CategoryBuilder)
    app.register(RecordBuilder)
    try:
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

            expanded = await request(app, "GET", "/api/records?expand=category")
            assert expanded.status_code == 200
            assert expanded.json()["items"][0]["expanded"]["category"] is None

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
            assert list_operation["responses"]["422"]["content"]["application/problem+json"][
                "schema"
            ] == {"$ref": "#/components/schemas/Problem"}

    asyncio.run(scenario())
