from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI
from obox.apps import AppConfig
from obox.contrib.auth.apps import Auth
from obox.core.exceptions import ImproperlyConfigured
from obox.core.principal import Principal, ScopedIdentity, has_permission
from obox.routes import Route
from obox.runtime.config import RuntimeConfig
from obox.runtime.context import Context
from obox.views.generic import TemplateView
from obox.web.dependencies import api_context

from tests.runtime import build_runtime, sessions_for, track_checkouts


class ContextView(TemplateView):
    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        return {**super().get_context_data(**kwargs), "value": "allowed"}


def test_scoped_identity_requires_a_synchronous_permission_check() -> None:
    scope = "demo.read"
    check = AsyncMock(return_value=True)
    owner = SimpleNamespace(id=1, username="actor", is_authenticated=True, is_active=True)
    owner.has_perm = check
    identity = ScopedIdentity(owner, frozenset({scope}))

    assert identity.has_perm(scope) is False
    assert has_permission(identity, scope) is False
    check.assert_not_called()


@pytest.fixture
def configs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[AppConfig, ...]:
    package = f"route_apps_{uuid4().hex}"
    root = tmp_path / package
    root.mkdir()
    (root / "__init__.py").touch()
    monkeypatch.syspath_prepend(str(tmp_path))
    result = []
    for label in ("first", "second"):
        directory = root / label
        directory.mkdir()
        (directory / "__init__.py").touch()
        templates = directory / "templates" / label
        templates.mkdir(parents=True)
        for name in ("t1", "t2", "missing"):
            (templates / f"{name}.html").write_text("{{ title }}:{{ value }}")
        config = AppConfig(f"{package}.{label}", import_module(f"{package}.{label}"))
        config.routes = tuple(
            Route(
                f"/{name}",
                f"{label}/{name}.html",
                permission="custom_action1",
                view=ContextView,
            )
            for name in ("t1", "t2")
        )
        result.append(config)
    return tuple(result)


def configured(
    configs: tuple[AppConfig, ...], actor: Any = None, *, template_dirs: tuple[Path, ...] = ()
) -> RuntimeConfig:
    return RuntimeConfig(
        session_factory=sessions_for("sqlite+aiosqlite://"),
        principal_dependency=actor or (lambda: Principal()),
        csrf_secret="s" * 32,
        template_dirs=template_dirs,
    )


def test_routes_bind_once_without_mutating_shared_declarations(
    configs: tuple[AppConfig, ...],
) -> None:
    first, second = (
        build_runtime(FastAPI(), configured(configs), app_configs=configs) for _ in range(2)
    )
    for label in ("first", "second"):
        for name in ("t1", "t2"):
            bound = first.routes[label, name]
            assert bound.permission == f"{label}.pages.custom_action1"
            assert bound.path == f"/admin/{label}/{name}"
            assert second.routes[label, name] is not bound
    assert set(first.permissions) == {"first.pages.custom_action1", "second.pages.custom_action1"}
    assert configs[0].routes[0].permission == "custom_action1"
    assert configs[0].routes[0].path == "/t1"


@pytest.mark.parametrize(
    ("actor", "status"),
    [
        (Principal(), 401),
        (Principal(is_authenticated=True, is_active=False), 401),
        (Principal(is_authenticated=True), 403),
        (Principal(is_authenticated=True, permissions=frozenset({"custom_action1"})), 403),
        (
            Principal(
                is_authenticated=True,
                permissions=frozenset({"second.pages.custom_action1"}),
            ),
            403,
        ),
        (
            Principal(is_authenticated=True, permissions=frozenset({"first.pages.custom_action1"})),
            200,
        ),
    ],
)
def test_short_route_permissions_enforce_exact_grants(
    configs: tuple[AppConfig, ...], actor: Principal, status: int
) -> None:
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

    asyncio.run(scenario())


def test_route_name_path_and_template_do_not_define_permission(
    configs: tuple[AppConfig, ...],
) -> None:
    configs[0].routes = (Route("/reports", "first/t1.html", name="renamed", permission="export"),)
    obox = build_runtime(FastAPI(), configured(configs), app_configs=configs)
    assert obox.routes["first", "renamed"].permission == "first.pages.export"
    obox.check(strict=True)


def test_routes_without_permission_are_public(configs: tuple[AppConfig, ...]) -> None:
    configs[0].routes = (Route("/open", "first/t1.html"),)

    async def scenario() -> None:
        app = FastAPI()
        build_runtime(app, configured(configs), app_configs=configs)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            assert (await client.get("/admin/first/open")).status_code == 200

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "permission", ["", "_private", "two..parts", "a.b.c.d", " leading", "a-b", 42]
)
def test_invalid_route_permissions_fail(permission: Any) -> None:
    with pytest.raises(ImproperlyConfigured, match="permission"):
        Route("/report", "report.html", permission=permission)


@pytest.mark.parametrize("path", ["report", "/", "/bad/", "/a//b", "/../secret", "/a/{id}"])
def test_invalid_route_paths_fail(path: str) -> None:
    with pytest.raises(ImproperlyConfigured, match="path"):
        Route(path, "report.html")


def test_missing_template_fails_before_opening_database(configs: tuple[AppConfig, ...]) -> None:
    configs[0].routes = (Route("/missing", "first/absent.html"),)
    obox = build_runtime(FastAPI(), configured(configs), app_configs=configs)
    with (
        track_checkouts(obox.session_factory) as checkouts,
        pytest.raises(ImproperlyConfigured, match=r"first/absent\.html"),
    ):
        obox.check(strict=True)
    assert not checkouts


def test_dynamic_template_reference_is_diagnostic_not_a_startup_error(
    configs: tuple[AppConfig, ...],
) -> None:
    templates = Path(configs[0].path) / "templates" / "first"
    (templates / "dynamic.html").write_text("{% include selected_template %}")
    configs[0].routes = (Route("/dynamic", "first/dynamic.html"),)
    obox = build_runtime(FastAPI(), configured(configs), app_configs=configs)
    diagnostics = obox.check(strict=True)
    assert any(item.code == "dynamic_template_reference" for item in diagnostics)


def test_invalid_route_view_is_rejected() -> None:
    with pytest.raises(ImproperlyConfigured, match="TemplateView"):
        Route("/report", "report.html", view=object)  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["routers", "routes"])
def test_invalid_app_route_configuration_is_rejected(
    configs: tuple[AppConfig, ...], field: str
) -> None:
    setattr(configs[0], field, (object(),))
    with pytest.raises(ImproperlyConfigured, match=field):
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
        assert "security" not in schema["paths"]["/v0/first/public/ping"]["get"]
        assert schema["paths"]["/v0/first/private/report"]["get"]["security"] == [
            {"cookieAuth": []}
        ]
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            assert order == ["start"]
            assert (await client.get("/v0/first/public/ping")).json() == {"ok": True}
            assert (await client.get("/v0/first/private/report")).status_code == 401
            identities["principal"] = Principal(is_authenticated=True)
            assert (await client.get("/v0/first/private/report")).status_code == 403
            identities["principal"] = Principal(
                is_authenticated=True, permissions=frozenset({"first.pages.custom_action1"})
            )
            assert (await client.get("/v0/first/private/report")).json() == {"source": "api"}
        assert order == ["start", "stop"]

    asyncio.run(scenario())
