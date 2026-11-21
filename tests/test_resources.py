import asyncio
from decimal import Decimal
from importlib import import_module
from typing import Annotated, Any, Self

import httpx
import pytest
import sqlalchemy as sa
from fastapi import APIRouter, Depends, FastAPI, Request
from obox import Obox, Resource, permission_required
from obox._auth import Principal, ScopedIdentity
from obox.api import ModelApi
from obox.apps import AppConfig
from obox.builder import ModelBuilder
from obox.conf import settings
from obox.core.exceptions import ImproperlyConfigured
from obox.views.generic import DetailView
from pydantic import BaseModel, Field, field_validator, model_validator

from tests.test_forms_integration import Item, configured


class ItemInput(BaseModel):
    name: Annotated[str, Field(min_length=2)]
    price: Decimal
    quantity: Annotated[int, Field(ge=1)] = 1

    @field_validator("name")
    @classmethod
    def normalize(cls, value: str) -> str:
        return value.strip().lower()

    @model_validator(mode="after")
    def check_amount(self) -> Self:
        if self.quantity * self.price < 2:
            raise ValueError("Total must be at least 2.")
        return self


class Items(Resource[Item]):
    model = Item
    schema = ItemInput
    ordering = ("-name",)

    def get_queryset(self, actor: Any) -> sa.Select[tuple[Item]]:
        return super().get_queryset(actor).where(Item.name != "private")

    async def can(self, action: str, obj: Item | None, actor: Any) -> bool:
        return action != "delete" or actor.is_superuser

    def get_writable_fields(self, action: str, obj: Item | None, actor: Any) -> tuple[str, ...]:
        return self.writable if actor.is_superuser else ("name", "price")


def authenticated_app() -> FastAPI:
    app = FastAPI(openapi_url=None)

    @app.middleware("http")
    async def identity(request: Request, call_next: Any) -> Any:
        who = request.headers.get("x-test-user")
        permissions = frozenset(
            f"tests.{action}_item" for action in ("add", "view", "change", "delete")
        )
        actor = Principal(
            id=1,
            is_authenticated=bool(who),
            is_active=who != "inactive",
            is_superuser=who in {"admin", "scoped"},
            permissions=permissions,
        )
        if request.headers.get("authorization"):
            key = actor
            if who == "scoped":
                key = ScopedIdentity(actor, frozenset({"tests.view_item"}))
            request.state.obox_bearer_principal = key
            request.scope["user"] = key
        else:
            request.scope["user"] = actor
        return await call_next(request)

    return app


def test_resource_validation_permissions_and_defaults_are_shared() -> None:
    async def scenario() -> None:
        async with configured():
            app = authenticated_app()
            Obox(app).register(Items)
            patch_schema = app.openapi()["components"]["schemas"]["ItemPatch"]
            assert patch_schema["properties"]["name"]["minLength"] == 2
            assert "required" not in patch_schema
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://test",
                    headers={"x-test-user": "editor"},
                ) as client,
            ):
                html = await client.get("/items/add")
                assert html.status_code == 200
                cookie = client.cookies["csrftoken"]
                token = {"csrfmiddlewaretoken": cookie}
                auth = {"authorization": "Bearer test"}
                denied = await client.post("/items/add", data={"name": "aa", "price": "2"})
                assert denied.status_code == 403
                denied = await client.post("/api/items", json={"name": "aa", "price": "2"})
                assert denied.status_code == 403

                invalid = {"name": "Alpha", "price": "1"}
                rejected = await client.post("/items/add", data={**invalid, **token})
                assert "Total must be at least 2." in rejected.text
                rejected = await client.post("/api/items", headers=auth, json=invalid)
                assert rejected.status_code == 422
                assert "Total must be at least 2." in rejected.text
                for endpoint, headers, data in [
                    ("/items/add", {}, {**token, "name": "Alpha", "price": "2", "quantity": "3"}),
                    ("/api/items", auth, {"name": "Alpha", "price": "2", "quantity": 3}),
                ]:
                    payload = {"json": data} if endpoint.startswith("/api") else {"data": data}
                    assert (
                        await client.post(endpoint, headers=headers, **payload)
                    ).status_code == 403
                created = await client.post(
                    "/items/add", data={**token, "name": " Alpha ", "price": "2"}
                )
                assert (created.status_code, created.headers["location"]) == (
                    303,
                    "http://test/items",
                )
                stored = (await client.get("/api/items/1")).json()
                assert stored["name"] == "alpha"
                assert stored["quantity"] == 1

                rejected = await client.patch("/api/items/1", headers=auth, json={"price": "1"})
                assert rejected.status_code == 422
                assert "Total must" in rejected.text
                assert (
                    await client.patch("/api/items/1", headers=auth, json={"quantity": 2})
                ).status_code == 403
                assert (
                    await client.patch("/api/items/1", headers=auth, json={"price": None})
                ).status_code == 422
                assert (await client.delete("/api/items/1", headers=auth)).status_code == 403
                assert (await client.post("/items/1/delete", data=token)).status_code == 403
                assert (
                    await client.get("/api/items", headers={"x-test-user": "inactive"})
                ).status_code == 401
                assert (
                    await client.delete("/api/items/1", headers={**auth, "x-test-user": "scoped"})
                ).status_code == 403

                for name in ("private", "zeta"):
                    assert (
                        await client.post(
                            "/api/items", headers=auth, json={"name": name, "price": "2"}
                        )
                    ).status_code == 201
                listing = (await client.get("/api/items?includeTotal=true")).json()
                assert listing["total"] == 2
                assert [item["name"] for item in listing["items"]] == ["zeta", "alpha"]
                assert "private" not in (await client.get("/items")).text
                assert (await client.get("/api/items/2")).status_code == 404
                assert (await client.get("/items/2/view")).status_code == 404

    asyncio.run(scenario())


def test_app_registration_custom_builder_and_direct_adapters() -> None:
    class ItemApi(ModelApi[Item]):
        resource = Items
        include_router_methods = ("list", "retrieve")

    class ItemDetail(DetailView):
        resource = Items

        def get_context_data(self, **kwargs: Any) -> Any:
            context = super().get_context_data(**kwargs)
            context["label"] = context["object"].name
            return context

        def render_to_response(self, context: Any, **kwargs: Any) -> Any:
            from fastapi.responses import PlainTextResponse

            return PlainTextResponse(context["label"])

    class Builder(ModelBuilder):
        slug = "inventory"
        api_class = ItemApi
        detail_view = ItemDetail

    class Inventory(AppConfig):
        name = "tests"
        label = "tests"

        def ready(self) -> None:
            self.register(Items, builder=Builder, views=False)

    async def scenario() -> None:
        async with configured():
            settings.INSTALLED_APPS = [Inventory("tests", import_module("tests"))]
            app = authenticated_app()
            obox = Obox(app)
            obox.register(Items, builder=Builder, api=False)
            nested = APIRouter(prefix="/custom")
            nested.add_api_route(
                "/item/{pk}",
                ItemDetail.as_view(),
                methods=["GET"],
                dependencies=[Depends(permission_required("tests.view_item", html=True))],
            )
            nested.include_router(ItemApi().build_router("/items"))
            app.include_router(nested)
            schema = app.openapi()["paths"]
            assert set(schema["/api/tests/inventory"]) == {"get"}
            assert set(schema["/custom/items"]) == {"get"}
            assert "/api/inventory" not in schema
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://test",
                    headers={"x-test-user": "admin"},
                ) as client,
            ):
                assert (await client.get("/tests/inventory")).status_code == 404
                assert (await client.get("/api/tests/inventory")).status_code == 200
                await client.get("/inventory/add")
                assert (
                    await client.post(
                        "/inventory/add",
                        data={
                            "csrfmiddlewaretoken": client.cookies["csrftoken"],
                            "name": "one",
                            "price": "2",
                        },
                    )
                ).status_code == 303
                assert (await client.get("/inventory/1/view")).text == "one"
                assert (await client.get("/custom/item/1")).text == "one"
                assert "tests.view_item" in obox._runtime.permissions

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("path", "guard", "message"),
    [("/unguarded", False, "Missing route permission"), ("/{path:path}", True, "shadowed")],
)
def test_startup_audits_native_route_permissions_and_order(
    path: str, guard: bool, message: str
) -> None:
    async def scenario() -> None:
        async with configured():
            app = FastAPI(openapi_url=None)
            inner = APIRouter()
            dependencies = [Depends(permission_required("tests.access"))] if guard else []

            async def endpoint() -> dict[str, str]:
                return {"state": "ready"}

            inner.add_api_route(path, endpoint, methods=["GET"], dependencies=dependencies)
            outer = APIRouter()
            outer.include_router(inner)
            app.include_router(outer)
            Obox(app).register(Item)
            with pytest.raises(ImproperlyConfigured, match=message):
                async with app.router.lifespan_context(app):
                    pytest.fail("Invalid route configuration must fail at startup.")

    asyncio.run(scenario())
