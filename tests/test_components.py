from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar, cast
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from obox._auth import RBAC, Principal, ScopedIdentity
from obox._catalog import CatalogConfig
from obox._components import Component, ComponentQuery, Use
from obox._config import RuntimeConfig
from obox._context import Context
from obox._pages import Page
from obox._runtime import _Runtime
from obox.contrib.analytics import Analytics, Bar, Pivot, Sum
from obox.contrib.resources import Resource as ResourceDefinition
from obox.contrib.resources.api_schema import ApiRequest, ApiResponse
from obox.contrib.resources.apis import ApiOptions
from obox.contrib.resources.filters import FilterGroup, FilterTerm
from obox.core.exceptions import ImproperlyConfigured
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Record(Base):
    __tablename__ = "component_record"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int]
    region_name: Mapped[str]
    amount: Mapped[Decimal] = mapped_column(sa.Numeric(20, 2))


class Policy(RBAC[Record]):
    hidden = False

    def scope(self, action: str, ctx: Context) -> Any:
        return Record.tenant_id == ctx.principal.id

    def aggregate_fields(self, ctx: Context) -> frozenset[str]:
        return frozenset({"region_name", "amount"})

    def read_fields(self, ctx: Context, *, obj: Record | None = None) -> frozenset[str] | None:
        return frozenset({"id", "region_name"}) if self.hidden else None


class Records(ResourceDefinition[Record]):
    model = Record
    fields = ("id", "region_name", "amount")
    filter_fields = ("region_name", "amount")
    ordering = ("id",)
    policy = Policy()

    class ApiOptions(ApiOptions):
        enabled = False

    api_options = ApiOptions


class Totals(Bar):
    name = "totals"
    source = Records
    x = "region_name"
    y = Sum("amount")
    drill = True


class Matrix(Pivot):
    name = "matrix"
    source = Records
    rows = "region_name"
    measure = Sum("amount")
    drill = True


ACTOR = Principal(
    id=1,
    is_authenticated=True,
    permissions=frozenset({"metrics.records.list", "metrics.records.retrieve"}),
)
ROOT = "/api/metrics/components"


@asynccontextmanager
async def runtime(
    tmp_path: Path,
    *,
    components: tuple[Any, ...] = (Totals, Matrix),
    pages: tuple[Page, ...] = (),
    actor: Any = ACTOR,
    builder: type[ResourceDefinition[Any]] = Records,
) -> AsyncIterator[tuple[_Runtime, httpx.AsyncClient]]:

    class Config(CatalogConfig):
        name = "obox"
        label = "metrics"

    Config.builders, Config.components, Config.pages = ((builder,), components, pages)
    engine = create_async_engine(os.getenv("OBOX_TEST_DATABASE_URL", "sqlite+aiosqlite://"))
    namespace = "component_" + uuid4().hex if engine.dialect.name == "postgresql" else None
    if namespace:
        async with engine.begin() as conn:
            await conn.execute(sa.schema.CreateSchema(namespace))
        engine = engine.execution_options(schema_translate_map={None: namespace})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine).begin() as session:
        session.add_all(
            [
                Record(id=1, tenant_id=1, region_name="East", amount=Decimal("10.01")),
                Record(id=2, tenant_id=1, region_name="West", amount=Decimal("20.02")),
                Record(id=3, tenant_id=2, region_name="SECRET", amount=Decimal("999")),
            ]
        )
    obox = _Runtime(
        FastAPI(),
        settings=RuntimeConfig(
            session_factory=async_sessionmaker(engine, expire_on_commit=False),
            installed_apps=(Config,),
            principal_dependency=lambda: actor,
            default_policy=RBAC(),
            csrf_secret="a" * 32,
            template_dirs=(tmp_path,),
        ),
        features=(Analytics(),),
    )
    try:
        async with (
            obox.app.router.lifespan_context(obox.app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=obox.app), base_url="http://test"
            ) as client,
        ):
            yield (obox, client)
    finally:
        if namespace:
            async with engine.begin() as conn:
                await conn.execute(sa.schema.DropSchema(namespace, cascade=True))
        await engine.dispose()


def test_headless_api_and_python_service_with_crud_disabled(tmp_path: Path) -> None:

    async def scenario() -> None:
        async with runtime(tmp_path) as (obox, client):
            assert not obox.pages
            assert not any(
                getattr(route, "name", None) == "obox_analytics_static" for route in obox.app.routes
            )
            assert (await client.get("/api/metrics/records")).status_code == 404
            response = await client.post(ROOT + "/totals:query", json={})
            assert response.status_code == 200, response.text
            payload = response.json()
            assert "config" not in payload
            assert "SECRET" not in response.text
            assert [point["y"] for point in payload["series"][0]["points"]] == ["10.01", "20.02"]
            selected = await client.post(
                ROOT + "/totals:records",
                json={"selector": payload["series"][0]["points"][0]["selector"]},
            )
            assert selected.status_code == 200, selected.text
            assert selected.json()["records"] == [
                {"id": "1", "regionName": "East", "amount": "10.01"}
            ]
            filtered = await client.post(
                ROOT + "/matrix:query", json={"query": [["regionName", "eq.East"]]}
            )
            assert filtered.json()["total"] == "10.01"
            description = await client.get(ROOT + "/totals:describe")
            assert description.json()["capabilities"] == ["query", "records", "view"]
            schema = obox.app.openapi()
            assert schema["paths"][ROOT + "/totals:query"]["post"]["requestBody"]
            assert (await client.head(ROOT + "/totals:describe")).content == b""
            assert (await client.options(ROOT + "/totals:query")).headers[
                "allow"
            ] == "OPTIONS, POST"
            async with obox.uow(principal=ACTOR) as ctx:
                data = await obox.component("matrix", app_label="metrics").query(ctx=ctx)
                assert data.total == "30.03"
            with pytest.raises(ImproperlyConfigured, match="active"):
                await obox.component("matrix", app_label="metrics").query(ctx=ctx)
            with pytest.raises(TypeError):
                cast(dict[tuple[str, str], Any], obox.components)["other", "totals"] = (
                    obox.component("totals", app_label="metrics")
                )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("actor", "status"),
    [
        (Principal(), 401),
        (replace(ACTOR, is_active=False), 401),
        (replace(ACTOR, permissions=frozenset()), 403),
        (ScopedIdentity(ACTOR, frozenset()), 403),
        (ScopedIdentity(ACTOR, frozenset({"metrics.records.list"})), 200),
    ],
)
def test_component_permissions_and_token_scopes(tmp_path: Path, actor: Any, status: int) -> None:

    async def scenario() -> None:
        async with runtime(tmp_path, actor=actor) as (_, client):
            response = await client.post(ROOT + "/matrix:query", json={})
            assert response.status_code == status, response.text
            assert response.headers["cache-control"] == "private, no-store"

    asyncio.run(scenario())


def test_component_and_records_permissions_and_field_projection(tmp_path: Path) -> None:

    async def scenario() -> None:
        protected = Totals(
            permission="metrics.analytics.view", records_permission="metrics.analytics.inspect"
        )
        async with runtime(tmp_path, components=(protected,)) as (obox, client):
            assert (await client.post(ROOT + "/totals:query", json={})).status_code == 403
            actor = replace(ACTOR, permissions=ACTOR.permissions | {"metrics.analytics.view"})
            resolver = obox.settings.principal_dependency
            assert resolver is not None
            obox.app.dependency_overrides[resolver] = lambda: actor
            description = (await client.get(ROOT + "/totals:describe")).json()
            assert "records" not in description["capabilities"]
            assert description["presentation"]["drill"] is False
            result = await client.post(ROOT + "/totals:query", json={})
            token = result.json()["series"][0]["points"][0]["selector"]
            assert (
                await client.post(ROOT + "/totals:records", json={"selector": token})
            ).status_code == 403
            actor = replace(actor, permissions=actor.permissions | {"metrics.analytics.inspect"})
            assert (
                await client.post(ROOT + "/totals:records", json={"selector": token})
            ).status_code == 200
            obox.get_resource(Record).api_read = ("id", "region_name")
            assert (await client.post(ROOT + "/totals:query", json={})).status_code == 403
            assert "metrics.analytics.inspect" in obox.permissions

    asyncio.run(scenario())


def test_page_bindings_are_private_scoped_and_independent(tmp_path: Path) -> None:
    page = Page(
        "report",
        permission="metrics.pages.report",
        components=(
            (
                Use(Matrix, key="all"),
                Use(Matrix, key="east", filters={"region_name": "East"}),
                Bar(source=Records, x="region_name", y=Sum("amount")),
            ),
        ),
    )

    async def scenario() -> None:
        async with runtime(tmp_path, pages=(page,)) as (obox, client):
            local = "/admin/metrics/report/_components/"
            assert (await client.get(local + "east/data")).status_code == 403
            assert (await client.post(ROOT + "/matrix:query", json={})).status_code == 200
            actor = replace(ACTOR, permissions=ACTOR.permissions | {"metrics.pages.report"})
            resolver = obox.settings.principal_dependency
            assert resolver is not None
            obox.app.dependency_overrides[resolver] = lambda: actor
            html = await client.get("/vi/admin/metrics/report")
            assert html.status_code == 200, html.text
            assert local + "east/data" in html.text
            assert (await client.get(local + "all/data")).json()["total"] == "30.03"
            assert (await client.get(local + "east/data")).json()["total"] == "10.01"
            narrowed = await client.get(
                local + "east/data", params={"east.filter.region_name": "West"}
            )
            assert narrowed.json()["records"] == 0
            records = await client.get(local + "east/records", params={"row": '"West"'})
            assert "20.02" not in records.text
            assert "SECRET" not in records.text
            assert (await client.get(local + "all/data")).json()["total"] == "30.03"
            assert (await client.post(ROOT + "/component3:query", json={})).status_code == 404
            assert set(obox.components) == {("metrics", "totals"), ("metrics", "matrix")}
            assert Matrix.filters == FilterGroup()

    asyncio.run(scenario())


def test_chart_selections_are_bound_to_identity_filters_and_actor(tmp_path: Path) -> None:

    async def scenario() -> None:
        async with runtime(tmp_path, components=(Totals, Totals(name="other"))) as (obox, client):
            response = await client.post(
                ROOT + "/totals:query", json={"query": [["regionName", "eq.East"]]}
            )
            token = response.json()["series"][0]["points"][0]["selector"]
            for name, query in (("other", [["regionName", "eq.East"]]), ("totals", [])):
                assert (
                    await client.post(
                        ROOT + f"/{name}:records", json={"selector": token, "query": query}
                    )
                ).status_code == 422
            resolver = obox.settings.principal_dependency
            assert resolver is not None
            obox.app.dependency_overrides[resolver] = lambda: replace(ACTOR, id=2)
            assert (
                await client.post(
                    ROOT + "/totals:records",
                    json={"selector": token, "query": [["regionName", "eq.East"]]},
                )
            ).status_code == 422

    asyncio.run(scenario())


class EchoInput(ApiRequest):
    display_name: str


class EchoOutput(ApiResponse):
    display_name: str
    count: int


class Echo(Component):
    name = "echo"
    query_model = EchoInput
    result_model = EchoOutput
    state: ClassVar[list[str]] = []

    def get_data(self, query: EchoInput, ctx: Context) -> Any:
        self.state.append(query.display_name)
        return EchoOutput(display_name=query.display_name, count=len(self.state))


class AsyncEcho(Echo):
    name = "asyncEcho"

    async def get_data(self, query: EchoInput, ctx: Context) -> Any:
        return super().get_data(query, ctx)


def test_custom_api_schemas_hooks_and_request_isolation(tmp_path: Path) -> None:

    async def scenario() -> None:
        async with runtime(tmp_path, components=(Echo, AsyncEcho)) as (obox, client):
            for name in ("echo", "asyncEcho", "echo"):
                response = await client.post(ROOT + f"/{name}:query", json={"displayName": "hello"})
                assert response.status_code == 200, response.text
                assert response.json() == {"displayName": "hello", "count": 1}
            assert Echo.state == []
            for payload in ({}, {"display_name": "wrong"}, {"displayName": "ok", "other": True}):
                assert (await client.post(ROOT + "/echo:query", json=payload)).status_code == 422
            description = (await client.get(ROOT + "/echo:describe")).json()
            assert "displayName" in description["querySchema"]["properties"]
            assert description["capabilities"] == ["query"]
            paths = obox.app.openapi()["paths"]
            assert ROOT + "/echo:records" not in paths
            assert paths[ROOT + "/echo:query"]["post"]["responses"]["200"]["content"][
                "application/json"
            ]["schema"]["$ref"].endswith("EchoOutput")

    asyncio.run(scenario())


def test_template_components_are_local_escaped_and_isolated(tmp_path: Path) -> None:
    (tmp_path / "note.html").write_text("{{ value }}:{{ count }}")

    class Note(Component):
        template = "note.html"
        state: ClassVar[list[str]] = []

        async def get_context(self, ctx: Context) -> dict[str, Any]:
            self.state.append("one")
            return {"value": '<img src=x onerror="alert(1)">', "count": len(self.state)}

    async def scenario() -> None:
        async with runtime(
            tmp_path, components=(), pages=(Page("notes", components=((Note,),)),)
        ) as (obox, client):
            for _ in range(2):
                response = await client.get("/admin/metrics/notes")
                assert response.status_code == 200, response.text
                assert "&lt;img" in response.text
                assert "<img src=x" not in response.text
                assert ":1" in response.text
            assert not obox.components
            assert not any(
                getattr(route, "name", None) == "obox_analytics_static" for route in obox.app.routes
            )
            assert Note.state == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "component",
    [
        Bar(source=Records, x="region_name"),
        Bar(name="invalid_name", source=Records, x="region_name"),
        Bar(name="missingSource", x="region_name"),
        Bar(name="missingField", source=Records, x="missing"),
        Component(name="empty"),
        Component(name="missingTemplate", template="missing.html"),
        Component(name="incomplete", query_model=EchoInput),
    ],
)
def test_invalid_components_fail_before_database_startup(
    tmp_path: Path, component: Component
) -> None:

    class Config(CatalogConfig):
        name = "obox"
        label = "metrics"
        builders = (Records,)
        components = (component,)

    def build() -> None:
        obox = _Runtime(
            FastAPI(),
            settings=RuntimeConfig(
                database_url="sqlite+aiosqlite://",
                installed_apps=(Config,),
                principal_dependency=lambda: ACTOR,
                default_policy=RBAC(),
                csrf_secret="a" * 32,
            ),
            features=(Analytics(),),
        )
        assert obox.database.engine is None
        obox.check(strict=True)

    with pytest.raises(ImproperlyConfigured):
        build()


def test_constructor_overrides_snapshot_and_narrowing_hooks(tmp_path: Path) -> None:

    class Narrow(Totals):
        def get_statement(self, ctx: Context) -> Any:
            return sa.select(Record).where(Record.region_name.in_(("East", "SECRET")))

    declaration = Narrow(name="narrow", title="Narrow")

    async def scenario() -> None:
        async with runtime(tmp_path, components=(declaration,)) as (obox, client):
            declaration.x = "amount"
            declaration.title = "Changed"
            response = await client.post(ROOT + "/narrow:query", json={})
            assert response.json()["categories"] == ["East"]
            description = await client.get(ROOT + "/narrow:describe")
            assert description.json()["title"] == "Narrow"
            binding = obox.component("narrow", app_label="metrics")
            definition = binding.definition
            definition.title = "Changed copy"
            assert binding.definition.title == "Narrow"
            assert (
                await client.post(
                    ROOT + "/narrow:query", json={"query": [["regionName", "eq.SECRET"]]}
                )
            ).json()["records"] == 0

    asyncio.run(scenario())


def test_sourceful_custom_provider_uses_narrowed_read_service(tmp_path: Path) -> None:

    class CountOutput(ApiResponse):
        count: int

    class Count(Component):
        name = "count"
        source = Records
        filters = FilterGroup(children=(FilterTerm("region_name", "is", "East"),))
        query_model = ComponentQuery
        result_model = CountOutput

        async def get_data(self, query: ComponentQuery, ctx: Context) -> CountOutput:
            records = await self.resource.list(ctx=ctx)
            return CountOutput(count=len(records))

    async def scenario() -> None:
        async with runtime(tmp_path, components=(Count,)) as (_, client):
            response = await client.post(ROOT + "/count:query", json={})
            assert response.status_code == 200, response.text
            assert response.json() == {"count": 1}

    asyncio.run(scenario())


@pytest.mark.parametrize("control", ["select", "order", "expand", "limit", "offset", "cursor"])
def test_component_queries_cannot_change_server_shape(tmp_path: Path, control: str) -> None:

    async def scenario() -> None:
        async with runtime(tmp_path) as (_, client):
            response = await client.post(
                ROOT + "/totals:query", json={"query": [[control, "amount"]]}
            )
            assert response.status_code == 422, response.text

    asyncio.run(scenario())


def test_invalid_custom_output_is_a_server_error(tmp_path: Path) -> None:

    class Invalid(Echo):
        def get_data(self, query: EchoInput, ctx: Context) -> Any:
            return {"displayName": "ok", "count": "invalid"}

    async def scenario() -> None:
        async with runtime(tmp_path, components=(Invalid,)) as (_, client):
            response = await client.post(ROOT + "/echo:query", json={"displayName": "hello"})
            assert response.status_code == 500
            assert "count" not in response.json()["detail"]

    asyncio.run(scenario())


@pytest.mark.parametrize("problem", ["duplicate", "placement", "reserved", "filter", "plugin"])
def test_registry_conflicts_fail_before_serving(tmp_path: Path, problem: str) -> None:

    class Config(CatalogConfig):
        name = "obox"
        label = "metrics"
        builders = (Records,)
        components: tuple[Any, ...] = (Totals,)

    features: tuple[Analytics, ...] = (Analytics(),)
    if problem == "duplicate":
        Config.components = (Totals, Totals)
    elif problem == "placement":
        Config.pages = (Page("report", components=((Totals, Totals),)),)
    elif problem == "reserved":

        class Reserved(Records):
            slug = "components"

        Config.builders = (Reserved,)
    elif problem == "filter":
        Config.components = (Totals(filters={"missing": "value"}),)
    else:
        features = ()
    with pytest.raises(ImproperlyConfigured):
        _Runtime(
            FastAPI(),
            settings=RuntimeConfig(
                database_url="sqlite+aiosqlite://",
                installed_apps=(Config,),
                principal_dependency=lambda: ACTOR,
                default_policy=RBAC(),
                csrf_secret="a" * 32,
            ),
            features=features,
        )
