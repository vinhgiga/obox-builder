from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from decimal import Decimal
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import Any, ClassVar, cast
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from obox.apps import AppConfig
from obox.components.declarations import Component
from obox.contrib.analytics import Analytics, Bar, Pivot, Sum
from obox.core.exceptions import ImproperlyConfigured
from obox.core.principal import Principal, ScopedIdentity
from obox.db.filters import FilterGroup
from obox.routes import Route
from obox.runtime.config import RuntimeConfig
from obox.runtime.context import Context
from obox.runtime.runtime import _Runtime
from obox.web.identity import get_principal
from obox.web.schemas import ApiRequest, ApiResponse
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from tests.runtime import build_runtime, sessions_for


class Base(DeclarativeBase):
    pass


class Record(Base):
    __tablename__ = "component_record"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int]
    region_name: Mapped[str]
    amount: Mapped[Decimal] = mapped_column(sa.Numeric(20, 2))


FIELDS = ("id", "region_name", "amount")


class Selection:
    source = Record
    fields = FIELDS

    def get_queryset(self, ctx: Context) -> Any:
        return sa.select(Record).where(Record.tenant_id == ctx.principal.id)


class Totals(Selection, Bar):
    name = "totals"
    x = "region_name"
    y = Sum("amount")
    drill = True


class Matrix(Selection, Pivot):
    name = "matrix"
    rows = "region_name"
    measure = Sum("amount")
    drill = True


class ProtectedTotals(Totals):
    name = "protected"
    permission = "metrics.analytics.inspect"


ACTOR = Principal(
    id=1,
    is_authenticated=True,
    permissions=frozenset({"tests.view_record", "metrics.components.access"}),
)
ROOT = "/v0/metrics/components"


@asynccontextmanager
async def runtime(
    tmp_path: Path,
    *,
    components: tuple[Any, ...] = (Totals, Matrix),
    routes: tuple[Route, ...] = (),
    actor: Any = ACTOR,
) -> AsyncIterator[tuple[_Runtime, httpx.AsyncClient]]:

    config = AppConfig("obox", import_module("obox"))
    config.label = "metrics"
    config.components, config.routes = components, routes
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
    obox = build_runtime(
        FastAPI(),
        settings=RuntimeConfig(
            session_factory=async_sessionmaker(engine, expire_on_commit=False),
            principal_dependency=lambda: actor,
            csrf_secret="a" * 32,
            template_dirs=(tmp_path,),
        ),
        app_configs=(config,),
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
            assert not obox.routes
            assert not any(
                getattr(route, "name", None) == "obox_analytics_static" for route in obox.app.routes
            )
            assert (await client.get("/v0/metrics/records")).status_code == 404
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
        (ScopedIdentity(ACTOR, frozenset({"tests.view_record"})), 200),
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
            obox.app.dependency_overrides[get_principal] = lambda: actor
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
            assert "metrics.analytics.inspect" in obox.permissions

    asyncio.run(scenario())


def test_template_bindings_are_app_scoped_and_independent(tmp_path: Path) -> None:
    template_dir = tmp_path / "metrics"
    template_dir.mkdir(parents=True)
    (template_dir / "report.html").write_text(
        "{{ component('tests.test_components.Matrix', key='all') }}"
        "{{ component('tests.test_components.Matrix', key='east', "
        "filters={'region_name': 'East'}) }}"
    )
    route = Route("/report", "metrics/report.html", permission="report")

    async def scenario() -> None:
        async with runtime(tmp_path, routes=(route,)) as (obox, client):
            local = "/admin/metrics/_components/"
            assert (await client.get(local + "east/data")).status_code == 200
            assert (await client.get(local + "east/data")).json()["total"] == "10.01"
            assert (await client.get("/admin/metrics/report")).status_code == 403
            assert (await client.post(ROOT + "/matrix:query", json={})).status_code == 200
            actor = replace(ACTOR, permissions=ACTOR.permissions | {"metrics.pages.report"})
            resolver = obox.settings.principal_dependency
            assert resolver is not None
            obox.app.dependency_overrides[resolver] = lambda: actor
            obox.app.dependency_overrides[get_principal] = lambda: actor
            html = await client.get("/vi/admin/metrics/report")
            assert html.status_code == 200, html.text
            assert "/vi" + local + "east/data" in html.text
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
            assert obox._template_components["metrics", "all"] is not obox.component(
                "matrix", app_label="metrics"
            )
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
        module_name = f"component_test_{uuid4().hex}"
        module = ModuleType(module_name)
        module.Note = Note  # type: ignore[attr-defined]
        sys.modules[module_name] = module
        (tmp_path / "metrics").mkdir()
        (tmp_path / "metrics" / "notes.html").write_text(
            f"{{{{ component('{module_name}.Note') }}}}"
        )
        try:
            async with runtime(
                tmp_path,
                components=(),
                routes=(Route("/notes", "metrics/notes.html"),),
            ) as (obox, client):
                for _ in range(2):
                    response = await client.get("/admin/metrics/notes")
                    assert response.status_code == 200, response.text
                    assert "&lt;img" in response.text
                    assert "<img src=x" not in response.text
                    assert ":1" in response.text
                assert not obox.components
                assert not any(
                    getattr(route, "name", None) == "obox_analytics_static"
                    for route in obox.app.routes
                )
                assert Note.state == []
        finally:
            sys.modules.pop(module_name, None)

    asyncio.run(scenario())


def test_template_components_scan_extends_imports_and_includes(tmp_path: Path) -> None:
    calls: list[int] = []

    class Note(Component):
        template = "note.html"

        async def get_context(self, ctx: Context) -> dict[str, int]:
            calls.append(len(calls) + 1)
            return {"count": calls[-1]}

    module_name = f"component_scan_{uuid4().hex}"
    module = ModuleType(module_name)
    module.Note = Note  # type: ignore[attr-defined]
    sys.modules[module_name] = module
    (tmp_path / "note.html").write_text("<b>{{ count }}</b>")
    templates = tmp_path / "metrics"
    templates.mkdir()
    (templates / "page.html").write_text(
        "{% extends base_template %}"
        "{% from 'metrics/macros.html' import show %}"
        "{% block content %}{{ show() }}{% include 'metrics/include.html' %}{% endblock %}"
    )
    reference = f"{module_name}.Note"
    (templates / "macros.html").write_text(
        f"{{% macro show() %}}{{{{ component('{reference}') }}}}{{% endmacro %}}"
    )
    (templates / "include.html").write_text(f"{{{{ component('{reference}') }}}}")

    async def scenario() -> None:
        try:
            routes = (
                Route("/first", "metrics/page.html"),
                Route("/second", "metrics/page.html"),
            )
            async with runtime(tmp_path, components=(), routes=routes) as (obox, client):
                assert len(obox._template_components) == 1
                first = obox._route_components["metrics", "first"][0][1]
                second = obox._route_components["metrics", "second"][0][1]
                assert first is second
                page1 = await client.get("/admin/metrics/first")
                page2 = await client.get("/admin/metrics/second")
                assert page1.text.count("<b>1</b>") == 2
                assert page2.text.count("<b>2</b>") == 2
                assert calls == [1, 2]
        finally:
            sys.modules.pop(module_name, None)

    asyncio.run(scenario())


def test_template_component_keys_are_stable_from_literal_props(tmp_path: Path) -> None:
    (tmp_path / "metrics").mkdir()
    (tmp_path / "metrics" / "report.html").write_text(
        "{{ component('tests.test_components.Matrix', filters={'region_name': 'East'}) }}"
    )
    route = Route("/report", "metrics/report.html")
    normalized = json.dumps(
        {"filters": {"region_name": "East"}}, sort_keys=True, separators=(",", ":")
    ).encode()
    suffix = hashlib.sha1(normalized, usedforsecurity=False).hexdigest()[:8]
    expected = "matrixX" + suffix

    async def scenario() -> None:
        async with runtime(tmp_path, routes=(route,)) as (first, _):
            first_name = next(iter(first._template_components))[1]
        async with runtime(tmp_path, routes=(route,)) as (second, _):
            second_name = next(iter(second._template_components))[1]
        assert first_name == second_name == expected

    asyncio.run(scenario())


def test_template_component_data_requires_each_permission_layer(tmp_path: Path) -> None:
    (tmp_path / "metrics").mkdir()
    (tmp_path / "metrics" / "report.html").write_text(
        "{{ component('tests.test_components.ProtectedTotals') }}"
    )
    route = Route("/report", "metrics/report.html")
    actors = (
        (Principal(), 401),
        (Principal(is_authenticated=True, permissions=frozenset({"tests.view_record"})), 403),
        (
            Principal(
                id=1,
                is_authenticated=True,
                permissions=frozenset({"tests.view_record", "metrics.components.access"}),
            ),
            403,
        ),
        (
            Principal(
                id=1,
                is_authenticated=True,
                permissions=frozenset(
                    {
                        "tests.view_record",
                        "metrics.components.access",
                        "metrics.analytics.inspect",
                    }
                ),
            ),
            200,
        ),
    )

    async def scenario() -> None:
        for actor, status in actors:
            async with runtime(tmp_path, components=(), routes=(route,), actor=actor) as (
                _,
                client,
            ):
                response = await client.get("/admin/metrics/_components/protected/data")
                assert response.status_code == status, response.text

    asyncio.run(scenario())


def test_template_component_call_requires_a_scanned_reference(tmp_path: Path) -> None:
    (tmp_path / "metrics").mkdir()
    (tmp_path / "metrics" / "empty.html").write_text("Nothing here")
    route = Route("/empty", "metrics/empty.html")

    async def scenario() -> None:
        async with runtime(tmp_path, components=(), routes=(route,)) as (obox, _):
            request = type("Request", (), {"state": type("State", (), {})()})()
            template = obox.templates.env.from_string(
                '{{ component("tests.test_components.Matrix") }}'
            )
            with pytest.raises(ImproperlyConfigured, match="not declared"):
                template.render({"request": request})

    asyncio.run(scenario())


def test_nonliteral_component_call_fails_with_template_line(tmp_path: Path) -> None:
    (tmp_path / "metrics").mkdir()
    (tmp_path / "metrics" / "dynamic.html").write_text("first line\n{{ component(ref) }}")
    config = AppConfig("obox", import_module("obox"))
    config.label = "metrics"
    config.routes = (Route("/dynamic", "metrics/dynamic.html"),)
    obox = _Runtime(
        FastAPI(),
        settings=RuntimeConfig(
            session_factory=sessions_for("sqlite+aiosqlite://"),
            principal_dependency=lambda: ACTOR,
            csrf_secret="a" * 32,
            template_dirs=(tmp_path,),
        ),
        defer=True,
    )
    obox.add_app(config)
    obox._loading_apps = False
    obox.finish_apps()
    with pytest.raises(ImproperlyConfigured, match=r"metrics/dynamic\.html:2"):
        obox.bind_templates()
    assert not obox.session_factory.kw["bind"].sync_engine.pool.checkedout()


def test_component_import_fails_before_database_startup(tmp_path: Path) -> None:
    (tmp_path / "metrics").mkdir()
    (tmp_path / "metrics" / "missing.html").write_text(
        "{{ component('tests.test_components.MissingComponent') }}"
    )
    config = AppConfig("obox", import_module("obox"))
    config.label = "metrics"
    config.routes = (Route("/missing", "metrics/missing.html"),)
    obox = _Runtime(
        FastAPI(),
        settings=RuntimeConfig(
            session_factory=sessions_for("sqlite+aiosqlite://"),
            principal_dependency=lambda: ACTOR,
            csrf_secret="a" * 32,
            template_dirs=(tmp_path,),
        ),
        defer=True,
    )
    obox.add_app(config)
    obox._loading_apps = False
    obox.finish_apps()
    with pytest.raises(ImproperlyConfigured, match="Cannot import component"):
        obox.bind_templates()
    assert not obox.session_factory.kw["bind"].sync_engine.pool.checkedout()


@pytest.mark.parametrize(
    "component",
    [
        Bar(source=Record, fields=FIELDS, x="region_name"),
        Bar(name="invalid_name", source=Record, fields=FIELDS, x="region_name"),
        Bar(name="missingSource", x="region_name"),
        Bar(name="missingField", source=Record, fields=FIELDS, x="missing"),
        Component(name="empty"),
        Component(name="missingTemplate", template="missing.html"),
        Component(name="incomplete", query_model=EchoInput),
    ],
)
def test_invalid_components_fail_before_database_startup(
    tmp_path: Path, component: Component
) -> None:

    config = AppConfig("obox", import_module("obox"))
    config.label = "metrics"
    config.components = (component,)

    def build() -> None:
        obox = build_runtime(
            FastAPI(),
            settings=RuntimeConfig(
                session_factory=sessions_for("sqlite+aiosqlite://"),
                principal_dependency=lambda: ACTOR,
                csrf_secret="a" * 32,
            ),
            app_configs=(config,),
            features=(Analytics(),),
        )
        assert not obox.session_factory.kw["bind"].sync_engine.pool.checkedout()
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


def test_filtered_chart_cannot_escape_a_limited_source(tmp_path: Path) -> None:
    class Limited(Matrix):
        def get_queryset(self, ctx: Context) -> Any:
            return super().get_queryset(ctx).order_by(Record.id).limit(1)

    async def scenario() -> None:
        async with runtime(tmp_path, components=(Limited,)) as (_, client):
            result = await client.post(
                ROOT + "/matrix:query", json={"query": [["regionName", "eq.West"]]}
            )
            assert result.status_code == 200, result.text
            assert result.json()["records"] == 0

    asyncio.run(scenario())
