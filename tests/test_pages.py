import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib import import_module
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI
from obox._auth import Principal
from obox._config import RuntimeConfig
from obox._context import Context
from obox._dependencies import api_context
from obox._pages import Page
from obox.apps import AppConfig
from obox.contrib.auth import Auth
from obox.core.exceptions import ImproperlyConfigured

from tests.runtime import build_runtime


@pytest.fixture
def configs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[AppConfig, ...]:
    package = f"page_apps_{uuid4().hex}"
    root = tmp_path / package
    root.mkdir()
    (root / "__init__.py").touch()
    declarations = (
        Page("t1", permission="custom_action1"),
        Page("t2", permission="custom_action1"),
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    result = []
    for label in ("first", "second"):
        directory = root / label
        directory.mkdir()
        (directory / "__init__.py").touch()
        templates = directory / "templates" / label
        templates.mkdir(parents=True)
        for name in ("t1", "t2", "open", "signed_in"):
            (templates / f"{name}.html").write_text("{{ title }}:{{ value }}")
        config = AppConfig(f"{package}.{label}", import_module(f"{package}.{label}"))
        config.pages = declarations
        result.append(config)
    return tuple(result)


def configured(configs: tuple[AppConfig, ...], actor: Any = None) -> RuntimeConfig:
    return RuntimeConfig(
        database_url="sqlite+aiosqlite://",
        principal_dependency=actor or (lambda: Principal()),
        csrf_secret="s" * 32,
    )


def test_permissions_bind_once_without_mutating_shared_declarations(
    configs: tuple[AppConfig, ...],
) -> None:
    first, second = (
        build_runtime(FastAPI(), configured(configs), app_configs=configs) for _ in range(2)
    )
    for label in ("first", "second"):
        for name in ("t1", "t2"):
            bound = first.pages[label, name]
            assert bound.permission == f"{label}.pages.custom_action1"
            assert bound.path == f"/admin/{label}/{name}"
            assert second.pages[label, name] is not bound
    assert set(first.permissions) == {"first.pages.custom_action1", "second.pages.custom_action1"}
    assert configs[0].pages[0] is configs[1].pages[0]
    assert configs[0].pages[0].permission == "custom_action1"
    assert configs[0].pages[0].path is None


@pytest.mark.parametrize(
    ("actor", "status"),
    [
        (Principal(), 401),
        (Principal(is_authenticated=True, is_active=False), 401),
        (Principal(is_authenticated=True), 403),
        (Principal(is_authenticated=True, permissions=frozenset({"custom_action1"})), 403),
        (
            Principal(
                is_authenticated=True, permissions=frozenset({"second.pages.custom_action1"})
            ),
            403,
        ),
        (
            Principal(is_authenticated=True, permissions=frozenset({"first.pages.custom_action1"})),
            200,
        ),
    ],
)
def test_short_permissions_enforce_exact_grants_before_running_context(
    configs: tuple[AppConfig, ...], actor: Principal, status: int
) -> None:
    calls = []

    def context(request: Any, ctx: Context) -> dict[str, str]:
        calls.append(ctx.principal)
        return {"value": "allowed"}

    configs[0].pages = (
        Page("t1", permission="custom_action1", context=context),
        Page("t2", permission="first.pages.custom_action1", context=context),
    )

    async def scenario() -> None:
        app = FastAPI()
        build_runtime(app, configured(configs, lambda: actor), app_configs=configs)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            for name in ("t1", "t2"):
                response = await client.get(f"/admin/first/{name}")
                assert response.status_code == status
                if status == 200:
                    assert "allowed" in response.text
            if actor.is_authenticated and actor.is_active:
                catalog = await client.get("/admin")
                assert ('href="/admin/first"' in catalog.text) == (status == 200)
            assert len(calls) == (2 if status == 200 else 0)

    asyncio.run(scenario())


def test_page_names_paths_and_templates_do_not_define_permissions(
    configs: tuple[AppConfig, ...],
) -> None:
    configs[0].pages = (
        Page("renamed", path="/reports", template="first/t1.html", permission="export"),
    )
    obox = build_runtime(FastAPI(), configured(configs), app_configs=configs)
    assert obox.pages["first", "renamed"].permission == "first.pages.export"
    obox.check(strict=True)


def test_public_and_authenticated_pages(configs: tuple[AppConfig, ...]) -> None:
    configs[0].pages = (Page("open", public=True), Page("signed_in"))
    identities = {"principal": Principal()}

    async def scenario() -> None:
        app = FastAPI()
        build_runtime(
            app,
            configured(configs, lambda: identities["principal"]),
            app_configs=configs,
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            assert (await client.get("/admin/first/open")).status_code == 200
            assert (await client.get("/admin/first/signed_in")).status_code == 401
            identities["principal"] = Principal(is_authenticated=True)
            assert (await client.get("/admin/first/signed_in")).status_code == 200

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "permission", ["", "_private", "two..parts", "a.b.c.d", " leading", "a-b", 42]
)
def test_invalid_permission_declarations_fail(permission: Any) -> None:
    with pytest.raises(ImproperlyConfigured, match="permission"):
        Page("report", permission=permission)


def test_public_pages_cannot_require_permissions() -> None:
    with pytest.raises(ImproperlyConfigured, match="Public pages"):
        Page("report", public=True, permission="export")


def test_missing_template_fails_before_opening_database(
    configs: tuple[AppConfig, ...],
) -> None:
    configs[0].pages = (Page("missing"),)
    obox = build_runtime(FastAPI(), configured(configs), app_configs=configs)
    with pytest.raises(ImproperlyConfigured, match="missing"):
        obox.check(strict=True)
    assert obox.database.engine is None


@pytest.mark.parametrize("field", ["routers"])
def test_invalid_router_configuration_is_rejected(
    configs: tuple[AppConfig, ...], field: str
) -> None:
    setattr(configs[0], field, (object(),) if field == "routers" else APIRouter())
    with pytest.raises(ImproperlyConfigured, match="routers"):
        build_runtime(FastAPI(), configured(configs), app_configs=configs)


def test_api_routers_preserve_dependencies_lifespan_and_security_metadata(
    configs: tuple[AppConfig, ...],
) -> None:
    order = []
    identities = {"principal": Principal()}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        order.append("start")
        yield
        order.append("stop")

    public = APIRouter(prefix="/public", lifespan=lifespan)
    private = APIRouter(prefix="/private")

    @public.get("/ping")
    async def ping() -> dict[str, bool]:
        return {"ok": True}

    @private.get("/report")
    async def report(
        ctx: Annotated[Context, Depends(api_context(permission="first.pages.custom_action1"))],
    ) -> dict[str, str]:
        return {"source": ctx.source}

    configs[0].routers = (public, private)

    async def scenario() -> None:
        app = FastAPI()
        build_runtime(
            app,
            settings=configured(configs, lambda: identities["principal"]),
            app_configs=configs,
            features=(Auth(admin=False, secure=False),),
        )
        schema = app.openapi()
        assert "security" not in schema["paths"]["/api/first/public/ping"]["get"]
        assert schema["paths"]["/api/first/private/report"]["get"]["security"] == [
            {"cookieAuth": []}
        ]
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            assert order == ["start"]
            assert (await client.get("/api/first/public/ping")).json() == {"ok": True}
            assert (await client.get("/api/first/private/report")).status_code == 401
            identities["principal"] = Principal(is_authenticated=True)
            assert (await client.get("/api/first/private/report")).status_code == 403
            identities["principal"] = Principal(
                is_authenticated=True, permissions=frozenset({"first.pages.custom_action1"})
            )
            assert (await client.get("/api/first/private/report")).json() == {"source": "api"}
        assert order == ["start", "stop"]

    asyncio.run(scenario())
