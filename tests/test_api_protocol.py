from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from fastapi.middleware.cors import CORSMiddleware
from obox_builder import API, BatchOperation, ModelBuilder, Principal
from obox_builder.errors import BatchOperationError, BuilderValidationError
from sqlalchemy.orm import Mapped, mapped_column
from test_api_contract import STAFF, Base, application, request
from test_operations import WidgetBuilder, make_app


class NumericRecord(Base):
    __tablename__ = "protocol_numbers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)
    value: Mapped[float | None]
    number: Mapped[int | None]
    amount: Mapped[Decimal | None] = mapped_column(sa.Numeric(10, 2))


class Numbers(ModelBuilder[NumericRecord]):
    slug = "numbers"

    class API(API):
        upsert_fields = (("name",),)


INVALID = [
    ("value", "NaN"),
    ("value", "Infinity"),
    ("value", "-Infinity"),
    ("value", True),
    ("number", False),
    ("amount", True),
]


@pytest.mark.parametrize(("field", "value"), INVALID)
@pytest.mark.parametrize("operation", ["create", "update", "batch", "upsert"])
def test_numeric_http_validation_is_atomic(field: str, value: Any, operation: str) -> None:
    async def scenario() -> None:
        async with application(configured=Numbers) as app:
            row = (
                await request(
                    app, "POST", "/api/numbers", json={"name": "original", "value": "1.25"}
                )
            ).json()
            data = {field: value}
            if operation == "create":
                response = await request(app, "POST", "/api/numbers", json={"name": "bad", **data})
            elif operation == "update":
                response = await request(app, "PATCH", f"/api/numbers/{row['id']}", json=data)
            else:
                op = (
                    {"op": "upsert", "mergeOn": ["name"], "data": {"name": "original", **data}}
                    if operation == "upsert"
                    else {"op": "create", "data": {"name": "bad", **data}}
                )
                response = await request(
                    app,
                    "POST",
                    "/api/numbers:batchWrite",
                    json={"operations": [{"op": "create", "data": {"name": "first"}}, op]},
                )
            assert response.status_code == 422, response.text
            items = (await request(app, "GET", "/api/numbers")).json()["items"]
            assert len(items) == 1
            assert items[0]["value"] == 1.25

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("field", "value"),
    [*INVALID, ("value", float("nan")), ("value", float("inf")), ("value", float("-inf"))],
)
@pytest.mark.parametrize("operation", ["create", "update", "batch", "upsert"])
def test_numeric_service_validation_is_atomic(field: str, value: Any, operation: str) -> None:
    async def scenario() -> None:
        async with application(configured=Numbers) as app:
            service = app.service(NumericRecord)
            async with app.uow(principal=STAFF) as ctx:
                row = await service.create({"name": "original", "value": 1.25}, ctx=ctx)
                key = row.id

            async def mutate() -> None:
                async with app.uow(principal=STAFF) as ctx:
                    data = {field: value}
                    if operation == "create":
                        await service.create({"name": "bad", **data}, ctx=ctx)
                    elif operation == "update":
                        await service.update(key, data, ctx=ctx)
                    elif operation == "upsert":
                        await service.upsert(
                            BatchOperation(
                                "upsert", data={"name": "original", **data}, merge_on=("name",)
                            ),
                            ctx=ctx,
                        )
                    else:
                        await service.batch(
                            [
                                BatchOperation("create", data={"name": "first"}),
                                BatchOperation("create", data={"name": "bad", **data}),
                            ],
                            ctx=ctx,
                        )

            with pytest.raises((BuilderValidationError, BatchOperationError)):
                await mutate()
            async with app.uow(principal=STAFF) as ctx:
                assert await service.count(ctx=ctx) == 1
                assert (await service.get(key, ctx=ctx)).value == 1.25

    asyncio.run(scenario())


def test_numeric_output_and_openapi() -> None:
    async def scenario() -> None:
        async with application(configured=Numbers) as app:
            response = await request(
                app,
                "POST",
                "/api/numbers",
                json={"name": "valid", "value": "2.5", "amount": "3.25", "number": "7"},
            )
            assert response.status_code == 201
            assert response.json()["value"] == 2.5
            assert response.json()["number"] == 7
            assert response.json()["amount"] == "3.25"
            response = await request(
                app,
                "PATCH",
                f"/api/numbers/{response.json()['id']}",
                json={"value": None, "number": None, "amount": None},
            )
            assert response.status_code == 200
            assert response.json()["value"] is None
            schema = app.app.openapi()
            ref = schema["paths"]["/api/numbers"]["post"]["responses"]["201"]["content"][
                "application/json"
            ]["schema"]["$ref"]
            field = schema["components"]["schemas"][ref.rsplit("/", 1)[1]]["properties"]["value"]
            assert {item["type"] for item in field["anyOf"]} == {"number", "null"}

    asyncio.run(scenario())


@pytest.mark.parametrize("principal", [STAFF, Principal()])
def test_head_options_permissions_and_allow(principal: Principal) -> None:
    async def scenario() -> None:
        async with application(principal=principal) as app:
            for path in (
                "/api/records",
                "/api/records/1",
                "/api/records:describe",
                "/api:describe",
                "/api/records:queryOptions",
            ):
                get = await request(app, "GET", path)
                head = await request(app, "HEAD", path)
                assert head.status_code == get.status_code
                assert head.content == b""
                for header in ("content-type", "cache-control", "vary", "www-authenticate"):
                    assert head.headers.get(header) == get.headers.get(header)
            options = await request(app, "OPTIONS", "/api/records")
            assert options.status_code == (200 if principal.is_authenticated else 404)
            if options.status_code == 200:
                assert options.json()["slug"] == "records"
                assert set(options.headers["allow"].split(", ")) == {
                    "GET",
                    "HEAD",
                    "POST",
                    "OPTIONS",
                }
            action = await request(app, "OPTIONS", "/api/records:publish")
            assert action.status_code == 204
            assert action.content == b""
            assert set(action.headers["allow"].split(", ")) == {"POST", "OPTIONS"}
            response = await request(app, "PUT", "/api/records/1")
            assert response.status_code == 405
            assert set(response.headers["allow"].split(", ")) == {
                "GET",
                "HEAD",
                "PATCH",
                "DELETE",
                "OPTIONS",
            }

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("accept", "status"),
    [
        ("application/json;q=0.0", 406),
        ("application/json; q = 0.000", 406),
        ("application/json;q", 406),
        ("application/json;q=0, */*;q=1", 406),
        ("application/*;q=0.5", 200),
        ("invalid,application/json", 200),
        ("application/json;q=bad", 406),
        ("application/json;q=1.1", 406),
        ("a" * 8193, 406),
        (",".join(["application/json"] * 65), 406),
        ("application/json;extra=" + "a" * 256, 406),
    ],
)
def test_accept_negotiation(accept: str, status: int) -> None:
    async def scenario() -> None:
        async with application() as app:
            response = await request(app, "GET", "/api/records", headers={"Accept": accept})
            assert response.status_code == status

    asyncio.run(scenario())


def test_cors_and_host_routes() -> None:
    async def scenario() -> None:
        app, engine = await make_app(WidgetBuilder, principal=STAFF)
        app.app.add_middleware(
            CORSMiddleware, allow_origins=["https://client.test"], allow_methods=["GET", "POST"]
        )

        @app.app.get("/host")
        async def host() -> dict[str, bool]:
            return {"ok": True}

        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app.app), base_url="http://test"
            ) as client:
                response = await client.options(
                    "/api/widgets",
                    headers={
                        "Origin": "https://client.test",
                        "Access-Control-Request-Method": "POST",
                    },
                )
                assert response.status_code == 200
                assert response.headers["access-control-allow-origin"] == "https://client.test"
                assert (await client.get("/host")).json() == {"ok": True}
                assert (await client.options("/host")).status_code == 405
        finally:
            await engine.dispose()

    asyncio.run(scenario())
