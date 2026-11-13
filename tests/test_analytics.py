from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from obox_builder import Context, ModelBuilder, Obox, Principal, Settings
from obox_builder.auth import ScopedIdentity, StaffOnly
from obox_builder.builder.aggregates import Aggregation, Measure
from obox_builder.builder.filters import FilterGroup, FilterTerm
from obox_builder.contrib.analytics import Analytics, Chart, Dashboard, Pivot
from obox_builder.errors import ImproperlyConfigured
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Sale(Base):
    __tablename__ = "analytics_sale"
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int]
    distributor: Mapped[str | None]
    region: Mapped[str | None]
    amount: Mapped[Decimal | None] = mapped_column(sa.Numeric(18, 2))
    quantity: Mapped[int] = mapped_column(default=1)
    active: Mapped[bool] = mapped_column(default=True)
    occurred_on: Mapped[date] = mapped_column(default=date(2026, 1, 1))
    moment: Mapped[datetime] = mapped_column(
        sa.DateTime, default=datetime(2026, 1, 1, tzinfo=UTC).replace(tzinfo=None)
    )
    extra: Mapped[dict[str, Any] | None] = mapped_column(sa.JSON)


class Duplicate(Base):
    __tablename__ = "analytics_duplicate"
    id: Mapped[int] = mapped_column(primary_key=True)
    sale_id: Mapped[int] = mapped_column(sa.ForeignKey(Sale.id))


STAFF = Principal(id=1, is_authenticated=True, is_staff=True)


class Policy(StaffOnly[Sale]):
    granted = True
    hidden = False
    joined = False
    slow = False

    def aggregate_fields(self, ctx: Context) -> frozenset[str]:
        return (
            frozenset(
                {"distributor", "region", "amount", "quantity", "active", "occurred_on", "moment"}
            )
            if self.granted
            else frozenset()
        )

    def scope(self, action: str, ctx: Context) -> Any:
        return Sale.account_id == ctx.principal.id

    def read_fields(self, ctx: Context, *, obj: Sale | None = None) -> frozenset[str] | None:
        return (
            frozenset({"id", "distributor", "region"})
            if self.hidden or (obj and obj.quantity == 9)
            else None
        )

    async def allow(self, action: str, ctx: Context, *, obj: Sale | None = None) -> bool:
        return not (
            action == "retrieve" and obj is not None and obj.distributor == "B"
        ) and await super().allow(action, ctx, obj=obj)


class Sales(ModelBuilder[Sale]):
    fields = (
        "id",
        "account_id",
        "distributor",
        "region",
        "amount",
        "quantity",
        "active",
        "occurred_on",
        "moment",
        "extra",
    )
    filter_fields = ("distributor", "region", "amount", "occurred_on", "active")
    search_fields = ("distributor",)

    def get_statement(self, ctx: Context) -> Any:
        statement = super().get_statement(ctx)
        if self.policy.slow:  # type: ignore[union-attr]
            statement = statement.where(sa.func.pg_sleep(0.1).is_(None))
        return statement.join(Duplicate) if self.policy.joined else statement  # type: ignore[union-attr]


WIDGETS = (
    Pivot("pivot", "distributor", Measure("sum", "amount"), columns="region", title="Revenue"),
    Chart("bar", "bar", "distributor", Measure("sum", "amount")),
    Chart("region", "bar", "region", Measure("sum", "amount")),
    Chart("pie", "pie", "distributor", Measure("sum", "amount")),
    Chart("donut", "donut", "distributor", Measure("sum", "amount")),
    Chart("line", "line", "occurred_on", Measure("avg", "quantity")),
    Chart("moment", "line", "moment"),
    Pivot(
        "fixed", "distributor", filters=FilterGroup(children=(FilterTerm("region", "is", "East"),))
    ),
)


@asynccontextmanager
async def application(
    path: Path, **limits: Any
) -> AsyncIterator[tuple[Obox, httpx.AsyncClient, Policy]]:
    engine = create_async_engine(
        os.getenv("OBOX_TEST_DATABASE_URL", f"sqlite+aiosqlite:///{path / 'analytics.db'}")
    )
    namespace = "analytics_" + uuid4().hex if engine.dialect.name == "postgresql" else None
    if namespace:
        async with engine.begin() as conn:
            await conn.execute(sa.schema.CreateSchema(namespace))
        engine = engine.execution_options(schema_translate_map={None: namespace})
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        policy = Policy()
        app = Obox(
            FastAPI(),
            settings=Settings(
                session_factory=async_sessionmaker(engine, expire_on_commit=False),
                principal_dependency=lambda: STAFF,
                default_policy=policy,
                csrf_secret="a" * 32,
            ),
        )
        app.register(Sales)
        app.install(Analytics((Dashboard("sales", "Sales <report>", Sale, WIDGETS),), **limits))
        async with app.app.router.lifespan_context(app.app):
            async with app.uow(principal=STAFF) as ctx:
                values = [
                    ("A", "East", "10"),
                    ("A", "East", "10"),
                    ("A", "West", "40"),
                    ("B", "East", "30"),
                    ("B", "West", None),
                    (None, "East", "0"),
                    ("", "West", "-5"),
                ]
                ctx.session.add_all(
                    [
                        Sale(
                            id=i,
                            account_id=1,
                            distributor=r,
                            region=c,
                            amount=Decimal(v) if v is not None else None,
                        )
                        for i, (r, c, v) in enumerate(values, 1)
                    ]
                )
                ctx.session.add(
                    Sale(
                        id=8, account_id=2, distributor="SECRET", region="East", amount=Decimal(999)
                    )
                )
                await ctx.session.flush()
                ctx.session.add_all([Duplicate(sale_id=i) for i in range(1, 9) for _ in range(2)])
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app.app), base_url="http://test"
            ) as client:
                yield app, client, policy
    finally:
        if namespace:
            async with engine.begin() as conn:
                await conn.execute(sa.schema.DropSchema(namespace, cascade=True))
        await engine.dispose()


@pytest.mark.parametrize(
    ("operation", "total"),
    [
        ("count", 7),
        ("sum", "85"),
        ("avg", "14.166666666666666666666666666666666667"),
        ("min", "-5"),
        ("max", "40"),
    ],
)
def test_aggregates_and_totals(tmp_path: Path, operation: Any, total: Any) -> None:
    async def scenario() -> None:
        async with application(tmp_path) as (app, _, policy):
            query = Aggregation(
                "distributor",
                Measure(operation, None if operation == "count" else "amount"),
                "region",
            )
            async with app.uow(principal=STAFF) as ctx:
                result = await app.service(Sale).aggregate(query, ctx=ctx)
                assert result.records == 7
                assert result.rows == ["", "A", "B", None]
                assert result.columns == ["East", "West"]
                assert Decimal(str(result.total)) == Decimal(str(total))
                assert result.cells[0][0] == (0 if operation == "count" else None)
                if operation == "sum":
                    assert result.row_totals == [Decimal(-5), Decimal(60), Decimal(30), Decimal(0)]
                    assert result.column_totals == [Decimal(50), Decimal(35)]
                before = result.to_dict()
            policy.joined = True
            async with app.uow(principal=STAFF) as ctx:
                assert (await app.service(Sale).aggregate(query, ctx=ctx)).to_dict() == before

    asyncio.run(scenario())


def test_filters_weighted_average_empty_and_null(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with application(tmp_path) as (app, client, _):
            async with app.uow(principal=STAFF) as ctx:
                service = app.service(Sale)
                query = Aggregation(
                    "distributor",
                    Measure("avg", "amount"),
                    "region",
                    row_sort="value",
                    row_descending=True,
                )
                result = await service.aggregate(
                    query,
                    ctx=ctx,
                    filter_group=FilterGroup(
                        children=(FilterTerm("distributor", "contains", "A"),)
                    ),
                )
                assert result.total == Decimal(20)
                assert result.cells == [[Decimal(10), Decimal(40)]]
                empty = await service.aggregate(query, ctx=ctx, search="missing")
                assert empty.records == 0
                assert empty.rows == []
                assert empty.total is None
                count = await service.aggregate(Aggregation("region"), ctx=ctx, search="missing")
                assert count.total == 0
                null = await service.aggregate(
                    query,
                    ctx=ctx,
                    filter_group=FilterGroup(
                        children=(
                            FilterTerm("distributor", "is", "B"),
                            FilterTerm("region", "is", "West"),
                        )
                    ),
                )
                assert null.total is None
                assert null.records == 1
            response = await client.get(
                "/analytics/sales/fixed/data", params={"filter.distributor": "A"}
            )
            assert response.json()["total"] == 2
            assert (
                await client.get("/analytics/sales/fixed/data", params={"filter.region": "West"})
            ).json()["records"] == 0

    asyncio.run(scenario())


def test_http_dashboard_data_records_and_permissions(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with application(tmp_path) as (app, client, policy):
            page = await client.get("/analytics/sales")
            assert page.status_code == 200, page.text
            assert "Sales &lt;report&gt;" in page.text
            assert "data-widget" in page.text
            assert 'role="img"' in page.text
            assert page.text.count("data-widget-expand") == 8
            assert page.text.count("data-widget-menu") == 6
            assert page.text.count('data-widget-view="chart"') == 6
            assert page.text.count('data-widget-view="table"') == 6
            assert "View as table" in page.text
            assert "View as chart" in page.text
            localized = await client.get("/vi/analytics/sales")
            assert localized.status_code == 200
            assert 'data-url="/vi/analytics/sales/pivot/data"' in localized.text
            assert "Áp dụng bộ lọc" in localized.text
            assert "Xem dạng bảng" in localized.text
            assert "Xem dạng biểu đồ" in localized.text
            assert "Mở rộng" in localized.text
            localized_data = await client.get("/vi/analytics/sales/pivot/data")
            assert localized_data.status_code == 200, localized_data.text
            assert localized_data.headers["content-language"] == "vi"
            for file in ("analytics.js", "analytics.css", "chart.umd.js", "http.js"):
                assert (await client.get("/analytics/_static/" + file)).status_code == 200
            response = await client.get("/analytics/sales/pivot/data")
            assert response.status_code == 200, response.text
            assert response.headers["cache-control"] == "private, no-store"
            assert response.json()["total"] == "85.00"
            assert "SECRET" not in response.text
            rows = await client.get(
                "/analytics/sales/pivot/records",
                params={"row": '"A"', "column": '"East"', "limit": 1},
            )
            assert rows.status_code == 200, rows.text
            assert "Next" in rows.text
            assert "/admin/sales/1" in rows.text
            null = await client.get("/analytics/sales/pivot/records", params={"row": "null"})
            blank = await client.get("/analytics/sales/pivot/records", params={"row": '""'})
            assert "0.00" in null.text
            assert "-5.00" not in null.text
            assert "-5.00" in blank.text
            denied = await client.get("/analytics/sales/pivot/records", params={"row": '"B"'})
            assert denied.status_code == 200
            assert "/admin/sales/4" not in denied.text
            negative = await client.get("/analytics/sales/pie/data")
            assert negative.status_code == 422
            assert negative.json()["code"] == "negative_chart"
            assert negative.headers["cache-control"] == "private, no-store"
            policy.granted = False
            assert (await client.get("/analytics/sales/pivot/data")).status_code == 403
            assert (await client.get("/analytics/sales/pivot/records")).status_code == 403
            policy.granted, policy.hidden = True, True
            assert (await client.get("/analytics/sales/pivot/data")).status_code == 403
            policy.hidden = False
            dependency = app.settings.principal_dependency
            assert dependency is not None
            app.app.dependency_overrides[dependency] = lambda: Principal()
            assert (await client.get("/analytics/sales/pivot/data")).status_code == 401

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "params",
    [
        {"source": "other"},
        {"ordering": "amount"},
        {"row": "null"},
        {"filter.account_id": "2"},
        {"filter.amount": "invalid"},
        {"filter": '{"conjunction":"or","children":[{"field":"account_id","op":"is","value":2}]}'},
        {"filter_op.region": "is"},
        {"filter_op.region": "sql", "filter.region": "East"},
    ],
)
def test_rejects_invalid_queries(tmp_path: Path, params: Any) -> None:
    async def scenario() -> None:
        async with application(tmp_path) as (_, client, _):
            response = await client.get("/analytics/sales/pivot/data", params=params)
            assert response.status_code == 422, response.text

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "params",
    [{"row": "{"}, {"row": "[]"}, {"offset": -1}, {"limit": 0}, {"limit": 201}, {"limit": "bad"}],
)
def test_rejects_invalid_record_queries(tmp_path: Path, params: Any) -> None:
    async def scenario() -> None:
        async with application(tmp_path) as (_, client, _):
            assert (
                await client.get("/analytics/sales/pivot/records", params=params)
            ).status_code == 422

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "limits",
    [
        {"max_rows": 3},
        {"max_columns": 1},
        {"max_cells": 7},
        {"max_chart_groups": 3},
        {"max_pie_groups": 1},
    ],
)
def test_group_limits_do_not_return_partial_totals(tmp_path: Path, limits: Any) -> None:
    async def scenario() -> None:
        async with application(tmp_path, **limits) as (_, client, _):
            widget = (
                "bar"
                if "max_chart_groups" in limits
                else "pie"
                if "max_pie_groups" in limits
                else "pivot"
            )
            response = await client.get(f"/analytics/sales/{widget}/data")
            assert response.status_code == 422
            assert response.json()["code"] == "aggregate_limit"

    asyncio.run(scenario())


def test_boundaries_sorting_keys_and_masking(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with application(tmp_path, max_rows=4, max_columns=2, max_cells=8) as (
            app,
            client,
            policy,
        ):
            assert (await client.get("/analytics/sales/pivot/data")).status_code == 200
            assert (
                await client.get("/analytics/sales/pivot/data?search=A&search=B")
            ).status_code == 422
            async with app.uow(principal=STAFF) as ctx:
                result = await app.service(Sale).aggregate(
                    Aggregation(
                        "distributor",
                        Measure("sum", "amount"),
                        row_sort="value",
                        row_descending=True,
                    ),
                    ctx=ctx,
                )
                assert result.rows == ["A", "B", "", None]
                obj = await ctx.session.get(Sale, 1)
                assert obj
                obj.quantity = 9
                obj.distributor = '<img src=x onerror="alert(1)">'
            response = await client.get(
                "/analytics/sales/pivot/records",
                params={"row": json.dumps('<img src=x onerror="alert(1)">')},
            )
            assert "&lt;img" in response.text
            assert "<img src=x" not in response.text
            assert "10.00" not in response.text
            policy.joined = True
            first = await client.get("/analytics/sales/pivot/records?limit=2")
            second = await client.get("/analytics/sales/pivot/records?limit=2&offset=2")
            assert first.status_code == second.status_code == 200
            assert "/admin/sales/2" in first.text
            assert "/admin/sales/3" in second.text

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "query",
    [
        Aggregation("missing"),
        Aggregation("extra"),
        Aggregation("region", Measure("sum", "active")),
        Aggregation("region", columns="region"),
    ],
)
def test_invalid_fields_fail_at_installation(tmp_path: Path, query: Aggregation) -> None:
    async def scenario() -> None:
        async with application(tmp_path) as (app, _, _):
            with pytest.raises(ImproperlyConfigured):
                query.validate(app.get_builder(Sale))

    asyncio.run(scenario())


def test_declarations_and_default_denial() -> None:
    assert StaffOnly().aggregate_fields(None) == frozenset()  # type: ignore[arg-type]
    for create in (
        lambda: Measure("sum"),
        lambda: Measure("count", "amount"),
        lambda: Dashboard("Bad", "Title", Sale, WIDGETS),
        lambda: Dashboard("test", "Title", Sale, (WIDGETS[0], WIDGETS[0])),
        lambda: Analytics((), max_cells=0),
        lambda: Analytics((), prefix="/{user}"),
    ):
        with pytest.raises(ImproperlyConfigured):
            create()


def test_datetime_keys_account_and_api_field_scope(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with application(tmp_path) as (app, client, _):
            response = await client.get("/analytics/sales/moment/data")
            assert response.status_code == 200, response.text
            key = response.json()["rows"][0]
            assert key.endswith(("Z", "+00:00"))
            records = await client.get(
                "/analytics/sales/moment/records", params={"row": json.dumps(key)}
            )
            assert records.status_code == 200, records.text
            assert "SECRET" not in records.text
            dependency = app.settings.principal_dependency
            assert dependency is not None
            app.app.dependency_overrides[dependency] = lambda: Principal(
                id=2, is_authenticated=True, is_staff=True
            )
            assert (await client.get("/analytics/sales/pivot/data")).json()["total"] == "999.00"
            app.get_builder(Sale).api_read = ("id", "distributor", "region")
            app.app.dependency_overrides[dependency] = lambda: ScopedIdentity(
                STAFF, frozenset({"admin.sales.list"})
            )
            assert (await client.get("/analytics/sales/pivot/data")).status_code == 403
            assert (await client.get("/analytics/sales/pivot/records")).status_code == 403

    asyncio.run(scenario())


def test_reference_dashboard_totals_match_all_widgets(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with application(tmp_path) as (app, client, _):
            amounts = (
                ("49258.00", "43460.28", "26958.39"),
                ("50674.19", "43030.70", "28121.56"),
                ("53917.61", "41143.39", "27506.75"),
                ("48914.25", "40574.73", "25782.04"),
            )
            async with app.uow(principal=STAFF) as ctx:
                await ctx.session.execute(sa.delete(Duplicate))
                await ctx.session.execute(sa.delete(Sale))
                ctx.session.add_all(
                    [
                        Sale(
                            account_id=1,
                            distributor=f"D{i}",
                            region=f"R{j}",
                            amount=Decimal(amount),
                        )
                        for i, row in enumerate(amounts)
                        for j, amount in enumerate(row)
                    ]
                )
            for widget in ("pivot", "bar", "region", "pie", "donut"):
                response = await client.get(f"/analytics/sales/{widget}/data")
                assert response.status_code == 200, response.text
                data = response.json()
                assert Decimal(data["total"]) == Decimal("479341.89")
                assert data["records"] == 12
                assert sum(map(Decimal, data["row_totals"])) == Decimal("479341.89")

    asyncio.run(scenario())


@pytest.mark.skipif(
    "postgresql" not in os.getenv("OBOX_TEST_DATABASE_URL", ""),
    reason="PostgreSQL timeout integration",
)
def test_postgres_timeout_rolls_back_and_connection_recovers(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with application(tmp_path, timeout_ms=20) as (_, client, policy):
            policy.slow = True
            response = await client.get("/analytics/sales/pivot/data")
            assert response.status_code == 503, response.text
            assert response.json()["code"] == "aggregate_timeout"
            assert response.headers["cache-control"] == "private, no-store"
            policy.slow = False
            response = await client.get("/analytics/sales/pivot/data")
            assert response.status_code == 200, response.text
            assert response.json()["total"] == "85.00"
            response = await client.get("/analytics/sales/pivot/records")
            assert response.status_code == 200

    asyncio.run(scenario())
