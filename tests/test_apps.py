from __future__ import annotations

import asyncio
import importlib
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa
from fastapi import APIRouter, FastAPI, Request
from obox_builder import AppConfig, Context, ModelBuilder, Obox, Principal, Settings, View
from obox_builder.auth import RBAC
from obox_builder.contrib.analytics import Analytics, Chart, Dashboard, Measure
from obox_builder.contrib.auth import Auth
from obox_builder.errors import ImproperlyConfigured
from obox_builder.i18n import localize
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class First(Base):
    __tablename__ = "apps_first"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    amount: Mapped[int]


class Second(Base):
    __tablename__ = "apps_second"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    amount: Mapped[int]
    first_id: Mapped[int] = mapped_column(sa.ForeignKey(First.id))
    first: Mapped[First] = relationship(First)


First.__name__ = Second.__name__ = "Entry"


class FirstPolicy(RBAC[First]):
    def aggregate_fields(self, ctx: Context) -> frozenset[str]:
        return frozenset({"name", "amount"})


class FirstBuilder(ModelBuilder[First]):
    slug = "items"
    fields = ("name", "amount")
    label_field = "name"
    policy = FirstPolicy("polls.items")


class SecondPolicy(RBAC[Second]):
    def aggregate_fields(self, ctx: Context) -> frozenset[str]:
        return frozenset({"name", "amount"})


class SecondBuilder(ModelBuilder[Second]):
    slug = "items"
    fields = ("name", "amount", "first_id")
    label_field = "name"
    policy = SecondPolicy("revenue.items")

    class View(View):
        list_display = ("name", "first")
        detail_fields = ("name", "amount", "first_id")
        operations = ("create",)


STAFF = Principal(
    id=1,
    username="editor",
    is_authenticated=True,
    permissions=frozenset(
        f"{app}.items.{action}"
        for app in ("polls", "revenue")
        for action in ("list", "retrieve", "create", "update", "delete")
    ),
)


@pytest.fixture
def configs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[type[AppConfig], ...]:
    package = f"catalog_{uuid4().hex}"
    root = tmp_path / package
    for name in ("", "polls", "revenue"):
        directory = root / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "__init__.py").write_text("")
    templates = root / "polls/templates/polls"
    templates.mkdir(parents=True)
    (templates / "custom.html").write_text("{{ message }}:{{ request.state.language }}")
    static = root / "polls/static"
    static.mkdir()
    (static / "app.css").write_text(".polls { color: red; }")
    monkeypatch.syspath_prepend(str(tmp_path))
    pages = APIRouter()

    @pages.get("/custom", name="polls_custom", include_in_schema=False)
    async def custom(request: Request) -> Any:
        return request.app.state.obox.templates.TemplateResponse(
            request=request,
            name="polls/custom.html",
            context={"message": "module template"},
        )

    api = APIRouter()

    @api.get("/ping", name="polls_ping")
    async def ping(request: Request) -> dict[str, bool]:
        return {"shared": request.app.state.obox.app is request.app}

    class PollsConfig(AppConfig):
        name = f"{package}.polls"
        label = "polls"
        builders = (FirstBuilder,)
        dashboards = (
            Dashboard(
                "totals", "Totals", First, (Chart("sum", "bar", "name", Measure("sum", "amount")),)
            ),
        )
        ui_router = pages
        api_router = api

        async def get_navigation(
            self, obox: Obox, request: Request, ctx: Context
        ) -> list[dict[str, Any]]:
            return (
                [{"label": "Custom", "url": localize(request, "/polls/custom"), "kind": "page"}]
                if ctx.principal.has_perm("polls.items.list")
                else []
            )

    class RevenueConfig(AppConfig):
        name = f"{package}.revenue"
        label = "revenue"
        builders = (SecondBuilder,)

    importlib.import_module(PollsConfig.name).Config = PollsConfig
    return PollsConfig, RevenueConfig


@asynccontextmanager
async def runtime(
    tmp_path: Path,
    configs: tuple[type[AppConfig], ...],
    *,
    actor: Principal = STAFF,
    root_path: str = "",
    plugins: tuple[Any, ...] = (),
    template_dirs: tuple[Path, ...] = (),
) -> AsyncIterator[tuple[Obox, httpx.AsyncClient, dict[str, Principal]]]:
    actors = {"principal": actor}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        obox = app.state.obox
        async with obox.database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with obox.session_factory.begin() as session:
            session.add(First(id=1, name="First", amount=10))
            session.add(Second(id=1, name="Second", amount=20, first_id=1))
        yield

    app = FastAPI(lifespan=lifespan)
    obox = Obox(
        app,
        settings=Settings(
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'apps.db'}",
            csrf_secret="x" * 32,
            ui_prefix="",
            installed_apps=configs,
            principal_dependency=lambda: actors["principal"],
            default_policy=RBAC("polls.items"),
            template_dirs=template_dirs,
        ),
        plugins=plugins,
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, root_path=root_path), base_url="http://test"
        ) as client,
    ):
        yield obox, client, actors


@pytest.mark.parametrize("root_path", ["", "/prefix"])
def test_shared_runtime_namespaced_routes_and_openapi(
    tmp_path: Path, configs: tuple[type[AppConfig], ...], root_path: str
) -> None:
    async def scenario() -> None:
        async with runtime(tmp_path, configs, root_path=root_path, plugins=(Analytics(),)) as (
            obox,
            c,
            _,
        ):
            for path in ("/", "/polls", "/polls/items", "/revenue/items", "/api/polls/ping"):
                response = await c.get(root_path + path)
                assert response.status_code == 200, response.text
            home = await c.get(root_path + "/polls")
            assert re.search(
                rf'<div class="navbar-brand[^\"]*">\s*<a[^>]*href="{root_path}/"',
                home.text,
                re.DOTALL,
            )
            assert (await c.get(root_path + "/api/polls/ping")).json() == {"shared": True}
            assert (await c.get(root_path + "/vi/polls/custom")).text == "module template:vi"
            assert (await c.get(root_path + "/api/polls/items")).json()["items"][0][
                "name"
            ] == "First"
            assert (await c.get(root_path + "/api/revenue/items")).json()["items"][0][
                "name"
            ] == "Second"
            related = await c.get(root_path + "/revenue/items/1")
            assert f'href="{root_path}/polls/items/1"' in related.text
            metadata = (await c.get(root_path + "/api:describe")).json()["resources"]
            assert {r["slug"] for r in metadata} == {"polls/items", "revenue/items"}
            first_id = next(r for r in metadata if r["slug"] == "revenue/items")["fields"]
            assert next(f for f in first_id if f["name"] == "firstId")["reference"] == "polls/items"
            schema = obox.app.openapi()
            operations = [v["operationId"] for p in schema["paths"].values() for v in p.values()]
            assert len(operations) == len(set(operations))
            assert "/api/polls/items" in schema["paths"]
            assert "/api/revenue/items" in schema["paths"]
            assert not any(p == "/polls/items" for p in schema["paths"])
            with pytest.raises(ImproperlyConfigured, match="frozen"):
                obox.add_app(configs[0]())

    asyncio.run(scenario())


def test_catalog_permissions_create_only_and_revocation(
    tmp_path: Path, configs: tuple[type[AppConfig], ...]
) -> None:
    async def scenario() -> None:
        actor = Principal(
            id=1, is_authenticated=True, permissions=frozenset({"polls.items.create"})
        )
        async with runtime(
            tmp_path, configs, actor=actor, plugins=(Auth(secure=False), Analytics())
        ) as (
            _,
            c,
            actors,
        ):
            home = await c.get("/")
            assert 'href="/polls"' in home.text
            assert 'href="/revenue"' not in home.text
            assert 'href="/auth"' not in home.text
            page = await c.get("/polls")
            assert 'href="/polls/items/new"' in page.text
            assert 'href="/polls/items"' not in page.text
            assert (await c.get("/polls/items/new")).status_code == 200
            assert (await c.get("/api/polls/items")).status_code == 403
            assert (await c.get("/auth")).status_code == 404
            actors["principal"] = Principal(id=1, is_authenticated=True)
            assert "No applications available." in (await c.get("/")).text
            assert (await c.get("/polls")).status_code == 404
            actors["principal"] = Principal()
            response = await c.get("/vi/polls")
            assert response.status_code == 303
            assert response.headers["location"] == "/vi/auth/login?next=%2Fvi%2Fpolls"

    asyncio.run(scenario())


def test_local_templates_assets_custom_routers_and_project_override(
    tmp_path: Path, configs: tuple[type[AppConfig], ...]
) -> None:
    override = tmp_path / "override/polls"
    override.mkdir(parents=True)
    (override / "custom.html").write_text("project override:{{ request.state.language }}")

    async def scenario() -> None:
        async with runtime(
            tmp_path, configs, plugins=(Analytics(),), template_dirs=(override.parent,)
        ) as (obox, c, _):
            assert (await c.get("/vi/polls/custom")).text == "project override:vi"
            assert (await c.get("/polls/_static/app.css")).text == ".polls { color: red; }"
            assert (
                str(obox.app.url_path_for("obox_polls_static", path="app.css"))
                == "/polls/_static/app.css"
            )
            page = await c.get("/polls/items")
            local_nav = page.text.split('aria-label="Resources"', 1)[1].split("</nav>", 1)[0]
            assert "/revenue/items" not in local_nav
            assert "/polls/custom" in local_nav
            assert (
                await c.get("/api/polls/items", headers={"Accept-Language": "vi"})
            ).status_code == 200
            assert (await c.get("/api/polls/items")).headers["content-language"] == "en"
            assert (await c.get("/polls/dashboards/totals")).status_code == 200
            data = await c.get("/polls/dashboards/totals/sum/data")
            assert data.status_code == 200, data.text
            assert (await c.get("/analytics/totals")).status_code == 404

    asyncio.run(scenario())


def test_app_config_paths_and_runtime_instances_are_independent(
    configs: tuple[type[AppConfig], ...],
) -> None:
    settings = Settings(
        database_url="sqlite+aiosqlite://",
        installed_apps=(configs[0].name + ".Config", configs[1]),
        principal_dependency=lambda: STAFF,
        default_policy=RBAC("polls.items"),
        ui_prefix="",
    )
    first, second = Obox(FastAPI(), settings=settings), Obox(FastAPI(), settings=settings)
    assert first.apps["polls"] is not second.apps["polls"]
    assert first.get_builder(First) is not second.get_builder(First)
    assert first.get_builder(First).app is first
    assert second.get_builder(First).app is second
    assert first.database.engine is second.database.engine is None
    with pytest.raises(TypeError):
        first.apps["new"] = configs[0]()  # type: ignore[index]


def test_empty_installed_apps_still_provides_a_catalog(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with runtime(tmp_path, (), actor=Principal(id=1, is_authenticated=True)) as (_, c, _):
            response = await c.get("/")
            assert response.status_code == 200
            assert "No applications available." in response.text

    asyncio.run(scenario())


def test_dashboards_with_the_same_slug_are_scoped_to_their_applications(
    tmp_path: Path, configs: tuple[type[AppConfig], ...]
) -> None:
    class Revenue(configs[1]):
        dashboards = (
            Dashboard(
                "totals",
                "Revenue totals",
                Second,
                (Chart("sum", "bar", "name", Measure("sum", "amount")),),
            ),
        )

    async def scenario() -> None:
        async with runtime(tmp_path, (configs[0], Revenue), plugins=(Analytics(),)) as (_, c, _):
            for label, total in (("polls", 10), ("revenue", 20)):
                path = f"/{label}/dashboards/totals"
                page = await c.get(path)
                assert page.status_code == 200
                assert path + "/sum/data" in page.text
                response = await c.get(path + "/sum/data")
                assert response.status_code == 200
                assert response.json()["total"] == total
            assert (await c.get("/vi/api/polls/items")).status_code == 404

    asyncio.run(scenario())


def test_missing_dashboard_plugin_fails_startup(configs: tuple[type[AppConfig], ...]) -> None:
    obox = Obox(
        FastAPI(),
        settings=Settings(
            database_url="sqlite+aiosqlite://",
            ui_prefix="",
            installed_apps=configs,
            principal_dependency=lambda: STAFF,
            default_policy=RBAC("polls.items"),
        ),
    )
    with pytest.raises(ImproperlyConfigured, match="Analytics"):
        obox.check(strict=True)


@pytest.mark.parametrize("problem", ["duplicate", "missing", "label", "api", "invalid", "resource"])
def test_invalid_application_configuration_fails_early(
    configs: tuple[type[AppConfig], ...], problem: str
) -> None:
    first, second = configs

    class Broken(first):
        pass

    installed: tuple[Any, ...] = (Broken, second)
    if problem == "duplicate":
        installed = (Broken, first)
    elif problem == "missing":
        Broken.requires = ("missing",)
    elif problem == "label":
        Broken.label = "vi"
    elif problem == "api":
        Broken.ui_prefix = "/api"
    elif problem == "invalid":
        installed = ("not_a_real_package.apps.Missing",)
    else:
        Broken.builders = (FirstBuilder, FirstBuilder)
    with pytest.raises(ImproperlyConfigured):
        Obox(
            FastAPI(),
            settings=Settings(
                database_url="sqlite+aiosqlite://",
                installed_apps=installed,
                principal_dependency=lambda: STAFF,
                default_policy=RBAC("polls.items"),
                ui_prefix="",
            ),
        )
