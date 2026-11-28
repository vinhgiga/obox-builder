from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, time
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from obox.core.exceptions import ImproperlyConfigured
from obox.core.principal import Principal
from obox.runtime.config import RuntimeConfig
from obox.runtime.registration import Registration
from obox.runtime.runtime import _Runtime
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from tests.runtime import collection_filter, resource_for
from tests.storage import database

CUSTOMERS = "/api/customers"
INVOICES = "/api/invoices"
GROUPS = CUSTOMERS + ":groups"


class Base(DeclarativeBase):
    pass


class Customer(Base):
    __tablename__ = "api_query_customers"
    id: Mapped[int] = mapped_column(primary_key=True)
    first_name: Mapped[str]
    last_name: Mapped[str]
    city: Mapped[str]
    country: Mapped[str]
    state: Mapped[str | None]
    company: Mapped[str | None]


class Invoice(Base):
    __tablename__ = "api_query_invoices"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(sa.ForeignKey(Customer.id))
    invoice_date: Mapped[datetime]


class Status(Enum):
    draft = "draft"
    live = "live"


class TypedRow(Base):
    __tablename__ = "api_query_types"
    id: Mapped[int] = mapped_column(primary_key=True)
    note: Mapped[str | None]
    state: Mapped[Status | None]
    stage: Mapped[str | None] = mapped_column(sa.Enum("draft", "live", name="query_stage"))
    token_id: Mapped[UUID | None]
    whole: Mapped[int | None]
    decimal_value: Mapped[Decimal | None] = mapped_column(sa.Numeric(6, 2))
    float_value: Mapped[float | None]
    active: Mapped[bool | None]
    calendar_day: Mapped[date | None]
    clock_time: Mapped[time | None]
    utc_time: Mapped[time | None] = mapped_column(sa.Time(timezone=True))
    occurred_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    payload: Mapped[dict[str, Any] | None] = mapped_column(sa.JSON)


@asynccontextmanager
async def application(
    tmp_path: Path,
) -> AsyncIterator[tuple[httpx.AsyncClient, async_sessionmaker[AsyncSession], FastAPI]]:
    async with database(tmp_path / "queries.sqlite3", (Base.metadata,)) as sessions:
        app = FastAPI()
        runtime = _Runtime(
            app,
            settings=RuntimeConfig(
                api_prefix="/api",
                session_factory=sessions,
                principal_dependency=lambda: Principal(is_authenticated=True, is_superuser=True),
            ),
        )
        for model, slug in [(Customer, "customers"), (Invoice, "invoices"), (TypedRow, "types")]:
            Registration(model, slug, True, False).mount(runtime)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            yield client, sessions, app


async def load_chinook(sessions: async_sessionmaker[AsyncSession]) -> None:
    data = json.loads((Path(__file__).parent / "fixtures" / "chinook.json").read_text("utf-8"))
    async with sessions.begin() as session:
        session.add_all(
            Customer(
                id=i, first_name=first, last_name=last, city=city, country=c, state=s, company=co
            )
            for i, first, last, city, c, s, co in data["customers"]
        )
        await session.flush()
        session.add_all(
            Invoice(id=i, customer_id=c, invoice_date=datetime.fromisoformat(day))
            for i, c, day in data["invoices"]
        )


async def get(client: httpx.AsyncClient, url: str, **params: str | int) -> dict[str, Any]:
    response = await client.get(url, params=params)
    assert response.status_code == 200, (params, response.text)
    return cast(dict[str, Any], response.json())


def test_chinook_customer_queries_and_groups(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with application(tmp_path) as (client, sessions, app):
            await load_chinook(sessions)
            assert (await get(client, CUSTOMERS, includeTotal="true"))["total"] == 59
            async with sessions() as session:
                is_sp = ("state", "is", "SP")
                empty = ("state", "is_empty", None)
                other_state = ("state", "is_not", "SP")
                either = json.loads(collection_filter(empty, other_state))
                either["conjunction"] = "or"
                for expression, sql, count in [
                    (collection_filter(is_sp), "state = 'SP'", 3),
                    (collection_filter(empty), "state IS NULL", 29),
                    (collection_filter(other_state), "state <> 'SP'", 27),
                    (json.dumps(either), "state IS NULL OR state <> 'SP'", 56),
                ]:
                    expected = (
                        await session.scalars(
                            sa.select(Customer.id).where(sa.text(sql)).order_by(Customer.id)
                        )
                    ).all()
                    result = await get(client, CUSTOMERS, filter=expression, limit=100)
                    assert [row["id"] for row in result["items"]] == list(expected)
                    assert len(expected) == count

                for term in ("köhler", "KÖHLER", "francois"):
                    expected = (
                        await session.scalars(
                            sa.select(Customer.id)
                            .where(
                                sa.text(
                                    "lower(last_name) LIKE lower(:term) OR "
                                    "lower(first_name) LIKE lower(:term)"
                                ).bindparams(term=f"%{term}%")
                            )
                            .order_by(Customer.id)
                        )
                    ).all()
                    found = await get(client, CUSTOMERS, q=term)
                    assert [row["id"] for row in found["items"]] == list(expected)
                    if term in {"köhler", "francois"}:
                        assert list(expected) == ([2] if term == "köhler" else [])
                    elif session.get_bind().dialect.name == "sqlite":
                        assert not expected

                for direction in ("asc", "desc"):
                    expected = list(
                        await session.scalars(
                            sa.select(Customer.id).order_by(
                                sa.text(f"state {direction} NULLS LAST, id ASC")
                            )
                        )
                    )
                    parts = [
                        await get(
                            client,
                            CUSTOMERS,
                            ordering="-state" if direction == "desc" else "state",
                            limit=23,
                            offset=i,
                        )
                        for i in (0, 23, 46)
                    ]
                    assert [part["hasMore"] for part in parts] == [True, True, False]
                    assert [row["id"] for part in parts for row in part["items"]] == expected

            result = await get(
                client,
                CUSTOMERS,
                q="köhler Stuttgart",
                filter=collection_filter(("country", "is", "Germany")),
            )
            assert [row["id"] for row in result["items"]] == [2]
            assert (
                await get(
                    client,
                    CUSTOMERS,
                    q="köhler Stuttgart",
                    filter=collection_filter(("country", "is", "USA")),
                )
            )["items"] == []

            countries = await get(client, GROUPS, group="country", includeTotal="true")
            assert countries["total"] == 24
            counts = {row["key"]["country"]: row["count"] for row in countries["items"]}
            assert (counts["USA"], counts["Canada"], counts["Brazil"]) == (13, 8, 5)

            expected_items = [
                {"key": {"country": c, "state": s}, "count": n}
                for c, s, n in [
                    ("Argentina", None, 1),
                    ("Brazil", "DF", 1),
                    ("Brazil", "RJ", 1),
                    ("Brazil", "SP", 3),
                ]
            ]
            assert await get(
                client,
                GROUPS,
                group="country,state",
                filter=collection_filter(("country", "in", ["Argentina", "Brazil"])),
            ) == {
                "by": ["country", "state"],
                "items": expected_items,
                "limit": 50,
                "offset": 0,
                "hasMore": False,
            }
            assert (
                await get(
                    client,
                    GROUPS,
                    group="state,country",
                    q="Stuttgart",
                    filter=collection_filter(("country", "is", "Germany")),
                )
            )["items"] == [{"key": {"state": None, "country": "Germany"}, "count": 1}]

            first = await get(client, GROUPS, group="country,state", limit=40, includeTotal="true")
            last = await get(client, GROUPS, group="country,state", limit=101, offset=40)
            assert (first["total"], first["hasMore"], len(first["items"])) == (42, True, 40)
            assert (last["limit"], last["hasMore"], len(last["items"])) == (100, False, 2)
            rows = first["items"] + last["items"]
            assert len({tuple(row["key"].values()) for row in rows}) == 42
            assert sum(row["count"] for row in rows) == 59
            assert (await get(client, GROUPS, group="country,state", limit=1, offset=42))[
                "items"
            ] == []

            paths = app.openapi()["paths"]
            assert {"filter", "q", "ordering", "limit"} <= {
                p["name"] for p in paths[CUSTOMERS]["get"]["parameters"]
            }
            group_by = next(p for p in paths[GROUPS]["get"]["parameters"] if p["name"] == "group")
            assert group_by["required"]
            assert group_by["description"]

    asyncio.run(scenario())


def test_chinook_invoice_filter_boundaries_and_clamp(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with application(tmp_path) as (client, sessions, _):
            await load_chinook(sessions)
            async with sessions() as session:
                sql_date = sa.DateTime()
                start = datetime.fromisoformat("2021-01-01")
                end = datetime.fromisoformat("2021-01-02")
                for op, value, sql in [
                    ("is", "2021-01-01T00:00:00", "invoice_date = :start"),
                    ("is_not", "2021-01-01T00:00:00", "invoice_date <> :start"),
                    ("gt", "2021-01-01T00:00:00", "invoice_date > :start"),
                    ("gte", "2021-01-01T00:00:00", "invoice_date >= :start"),
                    ("lt", "2021-01-01T00:00:00", "invoice_date < :start"),
                    ("lte", "2021-01-01T00:00:00", "invoice_date <= :start"),
                    (
                        "in",
                        ["2021-01-01T00:00:00", "2021-01-02T00:00:00"],
                        "invoice_date IN (:start, :end)",
                    ),
                    ("is", "2021-01-01T07:00:00+07:00", "invoice_date = :start"),
                    ("gte", "2021-01-01T00:00:00Z", "invoice_date >= :start"),
                ]:
                    predicate = sa.text(sql)
                    for name, bound in [
                        ("start", start),
                        ("end", end),
                        ("after", datetime.fromisoformat("2021-01-03")),
                    ]:
                        if f":{name}" in sql:
                            predicate = predicate.bindparams(
                                sa.bindparam(name, bound, type_=sql_date)
                            )
                    ids = list(
                        await session.scalars(
                            sa.select(Invoice.id).where(predicate).order_by(Invoice.id)
                        )
                    )
                    result = await get(
                        client,
                        INVOICES,
                        filter=collection_filter(("invoice_date", op, value)),
                        limit=101,
                        includeTotal="true",
                    )
                    assert result["total"] == len(ids)
                    assert [row["id"] for row in result["items"]] == ids[:100]
                    assert result["limit"] == 100
                    assert result["hasMore"] == (len(ids) > 100)
                assert result["total"] == 412

    asyncio.run(scenario())


def test_filter_operator_matrix_compares_to_sql(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with application(tmp_path) as (client, sessions, _):
            first = {
                "note": 'a,b ("c") \\ %_*[?]',
                "state": Status.draft,
                "stage": "draft",
                "token_id": UUID(int=1),
                "whole": 2,
                "decimal_value": Decimal("2.50"),
                "float_value": 2.5,
                "active": True,
                "calendar_day": date(2021, 1, 1),
                "clock_time": time(7),
                "utc_time": time(7, tzinfo=UTC),
                "occurred_at": datetime(2021, 1, 1, 16, tzinfo=UTC),
            }
            second = {
                "note": "",
                "state": Status.live,
                "stage": "live",
                "token_id": UUID(int=2),
                "whole": 3,
                "decimal_value": Decimal("3.50"),
                "float_value": 3.5,
                "active": False,
                "calendar_day": date(2021, 1, 2),
                "clock_time": time(8),
                "utc_time": time(8, tzinfo=UTC),
                "occurred_at": datetime(2021, 1, 2, tzinfo=UTC),
            }
            async with sessions.begin() as session:
                session.add_all([TypedRow(id=1, **first), TypedRow(id=2, **second), TypedRow(id=3)])
            async with sessions() as session:
                for field, token in [
                    ("note", first["note"]),
                    ("state", "draft"),
                    ("stage", "draft"),
                    ("token_id", str(first["token_id"])),
                    ("active", "true"),
                    ("whole", "2"),
                    ("decimal_value", "2.50"),
                    ("float_value", "2.5"),
                    ("calendar_day", "2021-01-01"),
                    ("clock_time", "07:00"),
                    ("utc_time", "07:00+00:00"),
                    ("occurred_at", "2021-01-01T23:00:00+07:00"),
                ]:
                    type_ = getattr(TypedRow, field).type
                    ops = {"is": "= :v", "is_not": "<> :v", "in": "IN (:v)"}
                    if field not in {"active", "note", "state", "stage", "token_id"}:
                        ops.update({"gt": "> :v", "gte": ">= :v", "lt": "< :v", "lte": "<= :v"})
                    for op, sql in ops.items():
                        raw = sa.text(f"{field} {sql}").bindparams(
                            sa.bindparam("v", first[field], type_=type_)
                        )
                        expected = await session.scalars(
                            sa.select(TypedRow.id).where(raw).order_by(TypedRow.id)
                        )
                        result = await get(
                            client,
                            "/api/types",
                            filter=collection_filter((field, op, [token] if op == "in" else token)),
                        )
                        assert [row["id"] for row in result["items"]] == list(expected), (field, op)

            for term, ids in [
                (("note", "is_empty", None), [2, 3]),
                (("note", "is_not_empty", None), [1]),
                (("note", "in", [""]), [2]),
                (("active", "is", True), [1]),
                (("active", "is", False), [2]),
                (("note", "contains", "%_*"), [1]),
                (("occurred_at", "in", ["2021-01-01T16:00:00Z", "2021-01-02T00:00:00Z"]), [1, 2]),
            ]:
                result = await get(client, "/api/types", filter=collection_filter(term))
                assert [row["id"] for row in result["items"]] == ids, term

            groups = await get(client, "/api/types:groups", group="-note,-state,-calendar_day")
            assert groups["items"] == [
                {
                    "key": {"note": first["note"], "state": "draft", "calendarDay": "2021-01-01"},
                    "count": 1,
                },
                {"key": {"note": "", "state": "live", "calendarDay": "2021-01-02"}, "count": 1},
                {"key": {"note": None, "state": None, "calendarDay": None}, "count": 1},
            ]

    asyncio.run(scenario())


def test_collection_query_validation(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with application(tmp_path) as (client, _, _):
            term = {"field": "id", "op": "is", "value": 1}
            group = {"conjunction": "and", "children": [term]}
            deep = json.dumps(
                {**group, "children": [{**group, "children": [{**group, "children": [group]}]}]}
            )
            for path, param, value in [
                (CUSTOMERS, "filter", "a" * 32_001),
                (CUSTOMERS, "filter", deep),
                (CUSTOMERS, "filter", collection_filter(*[("id", "is", 1)] * 51)),
                (CUSTOMERS, "filter", collection_filter(("id", "in", [1] * 101))),
                (CUSTOMERS, "filter", collection_filter(("id", "in", 1))),
                (CUSTOMERS, "filter", "country.eq.USA"),
                (CUSTOMERS, "filter", collection_filter(("firstName", "is", "test"))),
                (CUSTOMERS, "filter", collection_filter(("first_name", "gt", "test"))),
                (CUSTOMERS, "filter", collection_filter(("id", "is", "no"))),
                (CUSTOMERS, "ordering", "state desc"),
                (CUSTOMERS, "ordering", "-state,state"),
                (CUSTOMERS, "ordering", "id,first_name,last_name,city,state,country"),
                (CUSTOMERS, "q", "one " * 11),
                (CUSTOMERS, "q", "a" * 257),
                (CUSTOMERS, "q", "\0"),
                (CUSTOMERS, "limit", "0"),
                (CUSTOMERS, "limit", "1.5"),
                (CUSTOMERS, "offset", str(2**31)),
                (CUSTOMERS, "group", "state"),
                (CUSTOMERS, "cursor", "opaque"),
                (INVOICES, "filter", collection_filter(("invoice_date", "is", "2021-02-29"))),
                ("/api/types", "filter", collection_filter(("payload", "is_empty", None))),
                ("/api/types", "ordering", "payload"),
                ("/api/types:groups", "group", "payload"),
                (GROUPS, "group", "state,state"),
                (GROUPS, "group", "id,state,country,city"),
            ]:
                response = await client.get(path, params={param: value})
                assert response.status_code == 422, response.text
            for params in ({}, {"group": ""}, {"group": "state", "order": "state.asc"}):
                assert (await client.get(GROUPS, params=params)).status_code == 422

            for expression in [
                "",
                json.dumps({**group, "children": [group]}),
                collection_filter(*[("id", "is", 1)] * 50),
                collection_filter(("id", "in", [1] * 100)),
            ]:
                await get(client, CUSTOMERS, filter=expression)
            await get(
                client, CUSTOMERS, q="test " * 10, ordering="id,first_name,last_name,city,state"
            )

    asyncio.run(scenario())


def test_list_field_configuration() -> None:
    for attr, field in [
        ("filter_fields", "payload"),
        ("search_fields", "state"),
        ("ordering_fields", "payload"),
        ("group_fields", "occurred_at"),
        ("filter_fields", "missing"),
    ]:
        with pytest.raises(ImproperlyConfigured, match=field):
            resource_for(TypedRow, **{attr: (field,)})()
