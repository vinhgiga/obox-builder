import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import ModuleType
from typing import Any, Generic, TypeVar
from urllib.parse import urlencode

import httpx
import pytest
import sqlalchemy as sa
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from obox_builder import API, ModelBuilder, Obox, Principal, Settings
from obox_builder.auth import RBAC
from obox_builder.conf import load_settings
from obox_builder.contrib.activity import Activity
from obox_builder.contrib.auth import Auth
from obox_builder.contrib.auth.backends import ModelBackend
from obox_builder.contrib.files import Files
from obox_builder.db import Database
from obox_builder.errors import ImproperlyConfigured, OboxError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Item(Base):
    __tablename__ = "extension_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(50), unique=True)


class ItemBuilder(ModelBuilder[Item]):
    fields = ("id", "name")


TItem = TypeVar("TItem")


class BaseBuilder(ModelBuilder[TItem], Generic[TItem]):
    pass


class InheritedItemBuilder(BaseBuilder[Item]):
    fields = ("id", "name")


class LegacyItemBuilder(ModelBuilder[Item]):
    fields = ("id", "name")
    model = Item


STAFF = Principal(id=1, is_authenticated=True, is_staff=True, is_superuser=True)


def configured(**values: Any) -> Settings:
    return Settings(
        principal_dependency=lambda: STAFF,
        default_policy=RBAC(),
        csrf_secret="s" * 32,
        **values,
    )


async def request(app: FastAPI, method: str, path: str, **kwargs: Any) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.request(method, path, **kwargs)


def test_settings_module_selection_and_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    for name in ("setting", "alternate"):
        module = ModuleType(name)
        module.DATABASE_URL = "sqlite+aiosqlite://"  # type: ignore[attr-defined]
        module.SECRET_KEY = name * 32  # type: ignore[attr-defined]
        module.UI_PREFIX = f"/{name}/"  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.delenv("OBOX_SETTINGS_MODULE", raising=False)
    default = load_settings()
    assert default.ui_prefix == "/setting"
    monkeypatch.setenv("OBOX_SETTINGS_MODULE", "alternate")
    assert load_settings().ui_prefix == "/alternate"
    assert load_settings("setting").csrf_secret == "setting" * 32
    assert load_settings(default) is default
    with pytest.raises(FrozenInstanceError):
        default.ui_prefix = "/changed"  # type: ignore[misc]


def test_api_limits_inherit_settings_and_allow_explicit_overrides() -> None:
    class Unlimited(ItemBuilder):
        class API(API):
            default_limit = None
            max_limit = None
            max_batch = None

    for builder, expected in ((ItemBuilder, (100, 1000, 100)), (Unlimited, (None, None, None))):
        app = Obox(
            FastAPI(),
            settings=configured(
                database_url="sqlite+aiosqlite://",
                api_default_limit=100,
                api_max_limit=1000,
                api_max_batch=100,
            ),
        )
        app.register(builder)
        api = app.get_builder(Item).api
        assert (api.default_limit, api.max_limit, api.max_batch) == expected
        assert any(d.code == "api.unversioned_writes" for d in app.check())


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"database_url": ""},
        {"database_url": "sqlite+aiosqlite://", "session_factory": async_sessionmaker()},
        {"session_factory": object()},
        {"database_url": "sqlite+aiosqlite://", "ui_prefix": 3},
        {"database_url": "sqlite+aiosqlite://", "principal_dependency": "not_callable"},
        {"database_url": "sqlite+aiosqlite://", "default_policy": object()},
        {"database_url": "sqlite+aiosqlite://", "csrf_secret": "short"},
        {"database_url": "sqlite+aiosqlite://", "template_dirs": "templates"},
        {"database_url": "sqlite+aiosqlite://", "template_dirs": (3,)},
        {"database_url": "sqlite+aiosqlite://", "templates": object()},
        {"database_url": "sqlite+aiosqlite://", "base_template": ""},
        {"database_url": "sqlite+aiosqlite://", "api_default_limit": True},
        {"database_url": "sqlite+aiosqlite://", "api_max_limit": -1},
        {"database_url": "sqlite+aiosqlite://", "api_max_batch": "100"},
        {"database_url": "sqlite+aiosqlite://", "api_default_limit": 10, "api_max_limit": 5},
    ],
)
def test_invalid_settings_fail_early(values: dict[str, Any]) -> None:
    with pytest.raises(ImproperlyConfigured):
        Settings(**values)


def test_database_lifecycle_is_lazy_repeatable_and_preserves_factory() -> None:
    async def scenario() -> None:
        database = Database(Settings(database_url="sqlite+aiosqlite://"))
        sessions = database.sessions
        assert database.engine is None
        for _ in range(2):
            with pytest.raises(ImproperlyConfigured, match="lifecycle"):
                database.check()
            async with database.lifespan():
                assert database.sessions is sessions
                async with sessions() as session:
                    assert await session.scalar(sa.text("select 1")) == 1
                with pytest.raises(ImproperlyConfigured, match="already active"):
                    async with database.lifespan():
                        pass
            assert sessions.kw["bind"] is None
            assert database.engine is None

    asyncio.run(scenario())


@pytest.mark.parametrize("root_path", ["", "/prefix", "/auth"])
@pytest.mark.parametrize("override_router", [False, True])
def test_custom_login_and_shared_authentication_responses(
    tmp_path: Path, root_path: str, override_router: bool
) -> None:
    async def scenario() -> None:
        app = FastAPI()
        obox = Obox(
            app,
            settings=Settings(
                database_url="sqlite+aiosqlite://", ui_prefix="/console", api_prefix="/data"
            ),
        )
        routes = APIRouter()

        @routes.get("/login", name="obox_login")
        async def custom_login() -> HTMLResponse:
            return HTMLResponse("Custom sign in")

        @routes.post("/logout", name="obox_logout")
        async def custom_logout() -> HTMLResponse:
            return HTMLResponse("Signed out")

        class CustomAuth(Auth):
            def get_router(self, app: Obox) -> APIRouter:
                return routes

        obox.install(
            CustomAuth(secure=False) if override_router else Auth(router=routes, secure=False)
        )
        obox.install(Activity())
        obox.install(Files(root=tmp_path / "media", prefix="/media"))
        obox.register(ItemBuilder)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, root_path=root_path),
                base_url="http://test",
            ) as client,
        ):
            login_url = f"{root_path}/login"
            challenge = f'Cookie realm="obox", cookie-name=obox_session, form-action="{login_url}"'
            for path in ("/console/items", "/console/items/1", "/console/items/1/fields/name"):
                response = await client.get(f"{root_path}/vi{path}", follow_redirects=True)
                assert response.status_code == 200
                assert response.url.path == login_url
                assert response.text == "Custom sign in"
                response = await client.get(
                    f"{root_path}/vi{path}",
                    headers={"X-Obox-Request": "true", "X-Obox-Fragment": "true"},
                )
                assert response.status_code == 401
                assert response.headers["www-authenticate"] == challenge
                assert response.headers["x-obox-redirect"] == login_url + "?" + urlencode(
                    {"next": f"{root_path}/vi{path}"}
                )
                assert response.headers["cache-control"] == "private, no-store"
                assert "location" not in response.headers
                assert "X-Obox-Request" in response.headers["vary"]
            for method, path, kwargs in (
                ("POST", "/vi/console/items/1/fields/name", {"data": {"value": "changed"}}),
                ("GET", "/data/items", {}),
                ("POST", "/data/items:query", {"json": {"query": []}}),
            ):
                response = await client.request(
                    method,
                    root_path + path,
                    headers={"X-Obox-Request": "true"},
                    **kwargs,
                )
                assert response.status_code == 401, (method, path, response.text)
                assert response.headers["www-authenticate"] == challenge
                assert response.headers["x-obox-redirect"] == login_url + "?" + urlencode(
                    {"next": root_path + path}
                )
                assert "location" not in response.headers
            response = await client.get(f"{root_path}/data/items")
            assert response.status_code == 401
            assert response.json()["code"] == "unauthenticated"
            assert response.headers["content-type"] == "application/problem+json"
            assert response.headers["www-authenticate"] == challenge
            assert "x-obox-redirect" not in response.headers
            for method, path, file_kwargs in (
                (
                    "POST",
                    "/media",
                    {"json": {"filename": "test.txt", "size": 1, "field": "items.file"}},
                ),
                ("PATCH", "/media/00000000-0000-0000-0000-000000000001", {"content": b"x"}),
            ):
                response = await client.request(
                    method, root_path + path, headers={"X-Obox-Request": "true"}, **file_kwargs
                )
                assert response.status_code == 401
                assert response.headers["www-authenticate"] == challenge
                assert response.headers["x-obox-redirect"] == login_url + "?" + urlencode(
                    {"next": root_path + path}
                )
            script = await client.get(f"{root_path}/console/_static/http.js")
            files_script = await client.get(f"{root_path}/media/_static/http.js")
            assert script.status_code == files_script.status_code == 200
            assert script.content == files_script.content
            schema = app.openapi()
            assert schema["components"]["securitySchemes"]["cookieAuth"] == {
                "type": "apiKey",
                "in": "cookie",
                "name": "obox_session",
            }
            assert schema["paths"]["/data/items"]["get"]["security"] == [{"cookieAuth": []}]

    asyncio.run(scenario())


def test_lifespan_order_state_late_router_and_owned_engine_cleanup() -> None:
    async def scenario() -> None:
        order = []

        @asynccontextmanager
        async def host_lifespan(app: FastAPI) -> AsyncIterator[dict[str, str]]:
            obox = app.state.obox
            async with obox.session_factory() as session:
                assert await session.scalar(sa.text("select 1")) == 1
            order.append("host_start")
            yield {"token": "host"}
            assert obox.database.engine is not None
            order.append("host_stop")

        @asynccontextmanager
        async def router_lifespan(app: FastAPI) -> AsyncIterator[dict[str, str]]:
            order.append("router_start")
            yield {"router_token": "router"}
            order.append("router_stop")

        app = FastAPI(lifespan=host_lifespan)
        obox = Obox(app, settings=configured(database_url="sqlite+aiosqlite://"))
        obox.register(ItemBuilder)
        app.include_router(APIRouter(lifespan=router_lifespan))
        for _ in range(2):
            async with app.router.lifespan_context(app) as state:
                assert state == {"token": "host", "router_token": "router"}
                with pytest.raises(ImproperlyConfigured, match="frozen"):
                    obox.register(ItemBuilder)
            assert obox.database.engine is None
        assert order == ["host_start", "router_start", "router_stop", "host_stop"] * 2

    asyncio.run(scenario())


def test_startup_failure_cleans_database() -> None:
    async def scenario() -> None:
        @asynccontextmanager
        async def failing(app: FastAPI) -> AsyncIterator[None]:
            async with app.state.obox.session_factory() as session:
                await session.scalar(sa.text("select 1"))
            if app.state.obox.database.engine is not None:
                raise RuntimeError("host startup failed")
            yield  # pragma: no cover

        app = FastAPI(lifespan=failing)
        obox = Obox(app, settings=Settings(database_url="sqlite+aiosqlite://"))
        with pytest.raises(RuntimeError, match="host startup"):
            async with app.router.lifespan_context(app):
                pass
        assert obox.database.engine is None
        assert obox.session_factory.kw["bind"] is None

    asyncio.run(scenario())


def test_borrowed_engine_is_not_disposed() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite://")
        disposed = []
        sa.event.listen(engine.sync_engine, "engine_disposed", lambda _: disposed.append(True))
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        app = FastAPI()
        obox = Obox(app, settings=configured(session_factory=sessions))
        try:
            async with app.router.lifespan_context(app):
                assert obox.session_factory is sessions
            assert disposed == []
            async with sessions() as session:
                assert await session.scalar(sa.text("select 1")) == 1
        finally:
            await engine.dispose()
        assert disposed == [True]

    asyncio.run(scenario())


def test_app_and_builder_instances_are_isolated() -> None:
    first, second = FastAPI(title="First"), FastAPI(title="Second")
    a = Obox(first, settings=configured(database_url="sqlite+aiosqlite://"))
    b = Obox(second, settings=configured(database_url="sqlite+aiosqlite://"))
    a.register(ItemBuilder)
    b.register(ItemBuilder)
    assert first.state.obox is a
    assert second.state.obox is b
    assert a.get_builder(Item) is not b.get_builder(Item)
    assert a.get_builder(Item).app is a
    assert b.get_builder(Item).app is b
    assert not hasattr(ItemBuilder, "app")
    assert first.title == "First"
    assert a.session_factory is not b.session_factory
    with pytest.raises(ImproperlyConfigured, match="already installed"):
        Obox(first, settings=a.settings)
    with pytest.raises(ImproperlyConfigured, match="already registered"):
        a.register(ItemBuilder)
    with pytest.raises(ImproperlyConfigured, match="model"):
        a.register(ModelBuilder)
    with pytest.raises(ImproperlyConfigured, match="ModelBuilder"):
        a.register(object)


def test_model_is_inferred_through_generic_builder_base() -> None:
    obox = Obox(FastAPI(), settings=configured(database_url="sqlite+aiosqlite://"))

    registration = obox.register(InheritedItemBuilder)

    assert registration.model is Item
    assert registration.builder.model is Item
    with pytest.raises(ImproperlyConfigured, match="no longer supported"):
        Obox(FastAPI(), settings=configured(database_url="sqlite+aiosqlite://")).register(
            LegacyItemBuilder
        )


def test_databases_and_units_of_work_are_isolated() -> None:
    async def scenario() -> None:
        a = Obox(FastAPI(), settings=configured(database_url="sqlite+aiosqlite://"))
        b = Obox(FastAPI(), settings=configured(database_url="sqlite+aiosqlite://"))
        a.register(ItemBuilder)
        b.register(ItemBuilder)
        async with a.app.router.lifespan_context(a.app), b.app.router.lifespan_context(b.app):
            for obox in (a, b):
                assert obox.database.engine is not None
                async with obox.database.engine.begin() as connection:
                    await connection.run_sync(Base.metadata.create_all)
            async with a.uow(principal=STAFF) as ctx:
                await a.service(Item).create({"name": "only in a"}, ctx=ctx)
                with pytest.raises(ImproperlyConfigured):
                    await b.service(Item).list(ctx=ctx)
            async with b.uow(principal=STAFF) as ctx:
                assert await b.service(Item).list(ctx=ctx) == []

    asyncio.run(scenario())


def test_installation_after_first_request_is_rejected() -> None:
    async def scenario() -> None:
        app = FastAPI()
        assert (await request(app, "GET", "/")).status_code == 404
        with pytest.raises(ImproperlyConfigured, match="before the application starts"):
            Obox(app, settings=Settings(database_url="sqlite+aiosqlite://"))
        assert not hasattr(app.state, "obox")

    asyncio.run(scenario())


@pytest.mark.parametrize("path", ["/api/items", "/vi/admin/items", "/admin/_static/x"])
def test_host_routes_added_after_installation_are_checked(path: str) -> None:
    app = FastAPI()
    obox = Obox(app, settings=configured(database_url="sqlite+aiosqlite://"))
    obox.register(ItemBuilder)

    @app.get(path)
    async def host_route() -> dict[str, bool]:
        return {"ok": True}

    with pytest.raises(ImproperlyConfigured, match="Route conflict"):
        obox.check(strict=True)


@pytest.mark.parametrize("root_path", ["/prefix", "/admin"])
def test_http_crud_scoped_errors_locale_static_and_openapi(root_path: str) -> None:
    async def scenario() -> None:
        app = FastAPI()
        obox = Obox(app, settings=configured(database_url="sqlite+aiosqlite://"))
        obox.register(ItemBuilder)

        @app.exception_handler(ValueError)
        async def host_error(request: Request, exc: ValueError) -> JSONResponse:
            return JSONResponse({"host": str(exc)}, status_code=418)

        @app.get("/native/{number}")
        async def native(number: int, request: Request) -> dict[str, Any]:
            return {"number": number, "language": getattr(request.state, "language", None)}

        @app.get("/vi/native")
        async def prefixed_host() -> dict[str, bool]:
            raise ValueError("host handler")

        @app.get("/vi/admin/host")
        async def host_in_ui_namespace(request: Request) -> dict[str, Any]:
            return {"path": request.url.path, "language": getattr(request.state, "language", None)}

        async with app.router.lifespan_context(app):
            assert obox.database.engine is not None
            async with obox.database.engine.begin() as connection:
                tables = await connection.run_sync(lambda conn: sa.inspect(conn).get_table_names())
                assert tables == []
                await connection.run_sync(Base.metadata.create_all)
            created = await request(app, "POST", "/api/items", json={"name": "first"})
            assert created.status_code == 201
            invalid = await request(app, "POST", "/api/items", json={})
            assert invalid.status_code == 422
            assert invalid.headers["content-type"] == "application/problem+json"
            duplicate = await request(app, "POST", "/api/items", json={"name": "first"})
            assert duplicate.status_code == 409
            native_invalid = await request(app, "GET", "/native/invalid")
            assert native_invalid.status_code == 422
            assert "detail" in native_invalid.json()
            native_ok = await request(app, "GET", "/native/1", headers={"accept-language": "vi"})
            assert native_ok.json()["language"] is None
            assert (await request(app, "GET", "/vi/native")).status_code == 418
            assert (await request(app, "GET", "/vi/admin/host")).json() == {
                "path": "/vi/admin/host",
                "language": None,
            }
            assert (await request(app, "GET", "/vi/api/items")).status_code == 404
            assert "/api/items" in app.openapi()["paths"]
            assert (await request(app, "GET", "/admin/_static/list.js")).status_code == 200
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, root_path=root_path),
                base_url="http://test",
            ) as client:
                redirect = await client.get(f"{root_path}/vi/admin/items/?ordering=name")
                assert redirect.status_code == 307
                assert redirect.headers["location"] == (
                    f"http://test{root_path}/vi/admin/items?ordering=name"
                )
                page = await client.get(f"{root_path}/vi/admin/items")
                assert page.status_code == 200
                assert 'lang="vi"' in page.text
                assert f'href="{root_path}/en/admin/items"' in page.text
                assert f'href="{root_path}/vi/admin/items/new"' in page.text
                asset = await client.get(f"{root_path}/vi/admin/_static/list.js")
                assert asset.status_code == 200
                for stylesheet in ("ui.css", "tabler.min.css"):
                    response = await client.get(f"{root_path}/vi/admin/_static/{stylesheet}")
                    assert response.status_code == 200
                    assert response.headers["content-type"].startswith("text/css")
            listed = await request(app, "GET", "/api/items")
            assert listed.status_code == 200
            assert listed.json() == {
                "items": [created.json()],
                "page": {
                    "limit": None,
                    "offset": 0,
                    "hasMore": False,
                    "nextCursor": None,
                    "total": None,
                },
            }

    asyncio.run(scenario())


def test_commit_failure_is_mapped_before_sending_response() -> None:
    async def scenario() -> None:
        app = FastAPI()
        obox = Obox(app, settings=configured(database_url="sqlite+aiosqlite://"))
        obox.register(ItemBuilder)

        @app.exception_handler(OboxError)
        async def host_error(request: Request, exc: OboxError) -> JSONResponse:
            return JSONResponse({"host": True}, status_code=418)

        def fail_commit(session: Any) -> None:
            raise sa.exc.IntegrityError("COMMIT", {}, RuntimeError("constraint conflict"))

        async with app.router.lifespan_context(app):
            assert obox.database.engine is not None
            async with obox.database.engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            sa.event.listen(AsyncSession.sync_session_class, "before_commit", fail_commit)
            try:
                response = await request(app, "POST", "/api/items", json={"name": "rollback"})
                assert response.status_code == 409
            finally:
                sa.event.remove(AsyncSession.sync_session_class, "before_commit", fail_commit)
            async with obox.session_factory() as session:
                assert await session.scalar(sa.select(sa.func.count()).select_from(Item)) == 0

    asyncio.run(scenario())


def test_auth_is_explicit_and_uses_runtime_without_overriding_host() -> None:
    app = FastAPI()
    obox = Obox(app, settings=Settings(database_url="sqlite+aiosqlite://"))
    with pytest.raises(ImproperlyConfigured, match="authentication"):
        obox.register(ItemBuilder)
    auth = Auth(secure=False)
    obox.install(auth)
    assert isinstance(auth.backend, ModelBackend)
    assert auth.backend.session_factory is obox.session_factory
    assert obox.settings.principal_dependency == auth.principal
    assert isinstance(obox.settings.default_policy, RBAC)
    obox.register(ItemBuilder)
    second = Obox(FastAPI(), settings=configured(database_url="sqlite+aiosqlite://"))
    original = second.settings
    second.install(Auth(secure=False))
    assert second.settings.principal_dependency is original.principal_dependency
    assert second.settings.default_policy is original.default_policy
    with pytest.raises(ImproperlyConfigured, match="already installed"):
        second.install(Auth())
    with pytest.raises(ImproperlyConfigured, match="already installed"):
        Obox(FastAPI(), settings=original).install(auth)


@pytest.mark.parametrize("root_path", ["", "/prefix", "/auth"])
@pytest.mark.parametrize(
    ("path", "fragment"),
    [
        ("/admin/items", False),
        ("/admin/items/1", False),
        ("/admin/items/1/fields/name", True),
    ],
)
def test_auth_redirect_and_login_work_without_database_queries(
    root_path: str, path: str, fragment: bool
) -> None:
    async def scenario() -> None:
        app = FastAPI()
        obox = Obox(app, settings=Settings(database_url="sqlite+aiosqlite://"))
        obox.install(Auth(secure=False))
        obox.install(Activity())
        obox.register(ItemBuilder)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, root_path=root_path),
                base_url="http://test",
                follow_redirects=True,
            ) as client,
        ):
            page = await client.get(
                f"{root_path}/vi{path}",
                headers={"X-Obox-Fragment": "true"} if fragment else {},
            )
            assert page.history[0].status_code == 303
            assert page.history[0].headers["location"] == f"{root_path}/vi/auth/login?" + urlencode(
                {"next": f"{root_path}/vi{path}"}
            )
            if path != "/admin/items":
                assert page.history[0].headers["cache-control"] == "private, no-store"
            assert page.status_code == 200
            assert page.url.path == f"{root_path}/vi/auth/login"
            assert page.url.params["next"] == f"{root_path}/vi{path}"
            assert 'lang="vi"' in page.text

    asyncio.run(scenario())
