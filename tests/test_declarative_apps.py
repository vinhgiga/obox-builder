import asyncio
from importlib import import_module
from typing import Any, ClassVar

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.responses import PlainTextResponse
from obox import Obox, permission_required
from obox.api import ModelApi
from obox.apps import AppConfig
from obox.conf import settings
from obox.contrib.auth.models import Base as AuthBase
from obox.contrib.auth.models import User
from obox.contrib.auth.passwords import hash_password
from obox.core.exceptions import ImproperlyConfigured
from obox.views.generic import DetailView, ListView, ModelViewSet

from tests.test_auth_api import PASSWORD, issue, login
from tests.test_forms_integration import Item, configured
from tests.test_resources import Items, authenticated_app

INVENTORY_API = "/v0/inventory/v01_form_items"


class InventoryConfig(AppConfig):
    name = "tests"
    label = "inventory"
    views: ClassVar[list[str]] = ["tests.test_resources.Items"]
    apis: ClassVar[list[type[Items]]] = [Items]


class ItemViews(ModelViewSet[Item]):
    resource = Items
    slug = "stock-items"


class ItemApi(ModelApi[Item]):
    resource = Items
    include_router_methods = ("list", "retrieve")


def test_installed_app_exposes_shared_resource_views_and_api() -> None:
    async def scenario() -> None:
        async with configured():
            settings.INSTALLED_APPS = ["tests.test_declarative_apps.InventoryConfig"]
            app = authenticated_app()
            Obox(app)
            assert INVENTORY_API in app.openapi()["paths"]
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://test",
                    headers={"x-test-user": "admin"},
                ) as client,
            ):
                assert (await client.get("/inventory/items/add")).status_code == 200
                data = {
                    "csrfmiddlewaretoken": client.cookies["csrftoken"],
                    "name": " Alpha ",
                    "price": "1.00",
                }
                assert (
                    "Total must be at least 2."
                    in (await client.post("/inventory/items/add", data=data)).text
                )
                created = await client.post("/inventory/items/add", data={**data, "price": "2.00"})
                assert (created.status_code, created.headers["location"]) == (
                    303,
                    "/inventory/items",
                )
                listing = await client.get(INVENTORY_API)
                assert [item["name"] for item in listing.json()["items"]] == ["alpha"]
                row = listing.json()["items"][0]
                assert "alpha" in (await client.get(f"/inventory/items/{row['id']}/view")).text

    asyncio.run(scenario())


@pytest.mark.parametrize("html", [True, False])
def test_app_declarations_enable_each_surface_independently(html: bool) -> None:
    class Inventory(AppConfig):
        label = "inventory"
        views: ClassVar[list[type[Items]]] = [Items] if html else []
        apis: ClassVar[list[type[Items]]] = [] if html else [Items]

    async def scenario() -> None:
        async with configured():
            settings.INSTALLED_APPS = [Inventory("tests", import_module("tests"))]
            app = authenticated_app()
            Obox(app)
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://test",
                    headers={"x-test-user": "admin"},
                ) as client,
            ):
                assert (await client.get("/inventory/items")).status_code == (200 if html else 404)
                assert (await client.get(INVENTORY_API)).status_code == (404 if html else 200)

    asyncio.run(scenario())


def test_custom_viewset_api_and_native_router_use_application_prefixes() -> None:
    class ItemDetail(DetailView):
        def render_to_response(self, context: Any, **kwargs: Any) -> PlainTextResponse:
            return PlainTextResponse(context["object"].name)

    class StockViews(ItemViews):
        detail_view = ItemDetail

    router = APIRouter()

    @router.get("/status", dependencies=[Depends(permission_required("tests.view_item"))])
    async def status() -> dict[str, str]:
        return {"state": "ready"}

    class Inventory(AppConfig):
        label = "inventory"
        view_prefix = "/manage/inventory"
        api_prefix = "/service/inventory"
        views: ClassVar[list[type[StockViews]]] = [StockViews]
        apis: ClassVar[list[type[ItemApi]]] = [ItemApi]
        routers: ClassVar[list[APIRouter]] = [router]

    async def scenario() -> None:
        async with configured():
            settings.INSTALLED_APPS = [Inventory("tests", import_module("tests"))]
            app = authenticated_app()
            Obox(app)
            schema = app.openapi()["paths"]
            assert set(schema["/service/inventory/stock-items"]) == {"get"}
            assert "/v0/inventory/stock-items" not in schema
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://test",
                    headers={"x-test-user": "admin"},
                ) as client,
            ):
                root = "/manage/inventory/stock-items"
                assert (await client.get(f"{root}/add")).status_code == 200
                created = await client.post(
                    f"{root}/add",
                    data={
                        "csrfmiddlewaretoken": client.cookies["csrftoken"],
                        "name": "Alpha",
                        "price": "2.00",
                    },
                )
                assert created.headers["location"] == root
                rest = await client.get("/service/inventory/stock-items")
                row = rest.json()["items"][0]
                assert (await client.get(f"{root}/{row['id']}/view")).text == "alpha"
                assert (await client.get("/service/inventory/status")).json() == {"state": "ready"}
                assert (
                    await client.get("/service/inventory/stock-items", headers={"x-test-user": ""})
                ).status_code == 401

    asyncio.run(scenario())


def test_custom_api_prefix_keeps_token_auth_on_installed_api_routes() -> None:
    router = APIRouter()
    guard = [Depends(permission_required("tests.view_item"))]

    async def status() -> dict[str, str]:
        return {"state": "ready"}

    router.add_api_route("/status", status, methods=["GET"], dependencies=guard)

    class Inventory(AppConfig):
        view_prefix = "/inventory/html"
        api_prefix = "/inventory"
        views: ClassVar[list[type[Items]]] = [Items]
        apis: ClassVar[list[type[Items]]] = [Items]
        routers: ClassVar[list[APIRouter]] = [router]

    async def scenario() -> None:
        async with configured():
            settings.INSTALLED_APPS = [
                "obox.contrib.auth",
                Inventory("tests", import_module("tests")),
            ]
            settings.AUTH = {"api_tokens": True, "secure": False}
            app = FastAPI(openapi_url=None)
            app.add_api_route("/inventory/raw", status, methods=["GET"], dependencies=guard)
            obox = Obox(app)
            sessions = obox._runtime.session_factory
            async with app.router.lifespan_context(app):
                async with sessions.kw["bind"].begin() as connection:
                    await connection.run_sync(AuthBase.metadata.create_all)
                async with sessions.begin() as session:
                    session.add(
                        User(
                            username="alice",
                            password_hash=await hash_password(PASSWORD),
                            is_superuser=True,
                        )
                    )
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    await login(client)
                    token = (await issue(client, ["tests.view_item"]))["token"]
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url="http://test",
                    headers={"authorization": f"Bearer {token}"},
                ) as bearer:
                    api_path = "/inventory/v01_form_items"
                    assert (await bearer.get(api_path)).status_code == 200
                    assert (await bearer.get("/inventory/status")).status_code == 200
                    assert (await bearer.get("/inventory/html/items")).status_code == 401
                    assert (await bearer.get("/inventory/raw")).status_code == 401
                    assert (await bearer.post(api_path, json={})).status_code == 403

    asyncio.run(scenario())


class ModelOnlyApi(ModelApi[Item]):
    model = Item


class ConflictingApi(ItemApi):
    slug = "different-items"


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"views": [Items, ItemViews]}, "declared twice"),
        ({"views": [ListView]}, "ModelResource or ModelViewSet"),
        ({"apis": [ModelOnlyApi]}, "must declare a ModelResource class"),
        ({"apis": "tests.test_resources.Items"}, "must be a list or tuple"),
        ({"views": ["tests.test_resources.Unknown"]}, "Cannot load"),
        ({"views": [ItemViews], "apis": [ConflictingApi]}, "same slug"),
        ({"view_prefix": "manage"}, "view_prefix must be an absolute URL path"),
        ({"api_prefix": "/{app}"}, "api_prefix must be an absolute URL path"),
    ],
)
def test_invalid_declarative_app_configuration_fails_at_startup(
    fields: dict[str, Any], message: str
) -> None:
    async def scenario() -> None:
        async with configured():
            config = type("Inventory", (AppConfig,), fields)
            settings.INSTALLED_APPS = [config("tests", import_module("tests"))]
            with pytest.raises(ImproperlyConfigured, match=message):
                Obox(authenticated_app())

    asyncio.run(scenario())
