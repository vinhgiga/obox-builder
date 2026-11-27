import asyncio
from decimal import Decimal
from importlib import import_module
from typing import Annotated, Any, ClassVar, Self

import httpx
import pytest
import sqlalchemy as sa
from fastapi import APIRouter, Depends, FastAPI, Request
from obox import ModelResource, Obox, permission_required
from obox.api import ModelApi
from obox.apps import AppConfig
from obox.conf import settings
from obox.core.exceptions import ImproperlyConfigured
from obox.core.principal import Principal, ScopedIdentity
from obox.db.engine import default_session_factory
from obox.views.generic import DetailView, ModelViewSet
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from tests.test_forms_integration import Item, configured


class AlbumBase(DeclarativeBase):
    pass


class Album(AlbumBase):
    __tablename__ = "album"
    album_id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(sa.String(160))
    artist_id: Mapped[int]


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


class Items(ModelResource[Item]):
    model = Item
    schema = ItemInput
    ordering = ("-name",)

    def get_queryset(self) -> sa.Select[tuple[Item]]:
        return super().get_queryset().where(Item.name != "private")

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
            key: Principal | ScopedIdentity = actor
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
            api_path = "/v0/v01_form_items"
            item_path = f"{api_path}/1"
            schema = app.openapi()
            assert api_path in schema["paths"]
            assert "/v0/items" not in schema["paths"]
            patch_schema = schema["components"]["schemas"]["ItemPatch"]
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
                denied = await client.post(api_path, json={"name": "aa", "price": "2"})
                assert denied.status_code == 403

                invalid = {"name": "Alpha", "price": "1"}
                rejected = await client.post("/items/add", data={**invalid, **token})
                assert "Total must be at least 2." in rejected.text
                rejected = await client.post(api_path, headers=auth, json=invalid)
                assert rejected.status_code == 422
                assert "Total must be at least 2." in rejected.text
                for endpoint, headers, data in [
                    ("/items/add", {}, {**token, "name": "Alpha", "price": "2", "quantity": "3"}),
                    (api_path, auth, {"name": "Alpha", "price": "2", "quantity": 3}),
                ]:
                    payload: dict[str, Any] = (
                        {"json": data} if endpoint.startswith("/v0") else {"data": data}
                    )
                    assert (
                        await client.post(endpoint, headers=headers, **payload)
                    ).status_code == 403
                created = await client.post(
                    "/items/add", data={**token, "name": " Alpha ", "price": "2"}
                )
                assert (created.status_code, created.headers["location"]) == (
                    303,
                    "/items",
                )
                stored = (await client.get(item_path)).json()
                assert stored["name"] == "alpha"
                assert stored["quantity"] == 1

                rejected = await client.patch(item_path, headers=auth, json={"price": "1"})
                assert rejected.status_code == 422
                assert "Total must" in rejected.text
                assert (
                    await client.patch(item_path, headers=auth, json={"quantity": 2})
                ).status_code == 403
                assert (
                    await client.patch(item_path, headers=auth, json={"price": None})
                ).status_code == 422
                assert (await client.delete(item_path, headers=auth)).status_code == 403
                assert (await client.post("/items/1/delete", data=token)).status_code == 403
                assert (
                    await client.get(api_path, headers={"x-test-user": "inactive"})
                ).status_code == 401
                assert (
                    await client.delete(item_path, headers={**auth, "x-test-user": "scoped"})
                ).status_code == 403

                for name in ("private", "zeta"):
                    assert (
                        await client.post(api_path, headers=auth, json={"name": name, "price": "2"})
                    ).status_code == 201
                listing = (await client.get(api_path, params={"includeTotal": "true"})).json()
                assert listing["total"] == 2
                assert [item["name"] for item in listing["items"]] == ["zeta", "alpha"]
                assert "private" not in (await client.get("/items")).text
                assert (await client.get(f"{api_path}/2")).status_code == 404
                assert (await client.get("/items/2/view")).status_code == 404

    asyncio.run(scenario())


def test_registered_model_api_supports_crud_and_camel_case_contract() -> None:
    async def scenario() -> None:
        async with configured():
            engine = default_session_factory().kw["bind"]
            async with engine.begin() as connection:
                await connection.run_sync(AlbumBase.metadata.create_all)
            app = authenticated_app()
            obox = Obox(app)
            obox.register(Album)
            async with default_session_factory().begin() as session:
                session.add(
                    Album(
                        title="For Those About To Rock We Salute You",
                        artist_id=1,
                    )
                )

            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://test",
                    headers={"x-test-user": "admin"},
                ) as client,
            ):
                created = await client.post(
                    "/v0/album/", json={"title": "New Album", "artistId": 1}
                )
                assert created.status_code == 201, created.text
                assert created.headers["content-type"].startswith("application/json")
                assert created.json() == {
                    "albumId": 2,
                    "title": "New Album",
                    "artistId": 1,
                }

                response = await client.get("/v0/album/1")
                assert response.status_code == 200, response.text
                assert response.json() == {
                    "albumId": 1,
                    "title": "For Those About To Rock We Salute You",
                    "artistId": 1,
                }

                patched = await client.patch("/v0/album/1", json={"title": "Updated Album"})
                assert patched.status_code == 200, patched.text
                assert patched.json() == {
                    "albumId": 1,
                    "title": "Updated Album",
                    "artistId": 1,
                }

                updated = await client.put(
                    "/v0/album/1", json={"title": "Updated Album", "artistId": 2}
                )
                assert updated.status_code == 200, updated.text
                assert updated.json() == {
                    "albumId": 1,
                    "title": "Updated Album",
                    "artistId": 2,
                }

                missing = await client.put(
                    "/v0/album/999", json={"title": "Missing", "artistId": 2}
                )
                assert missing.status_code == 404
                assert (await client.get("/v0/album/999")).status_code == 404
                missing_patch = await client.patch("/v0/album/999", json={"title": "Missing"})
                assert missing_patch.status_code == 404
                assert (await client.get("/v0/album/999")).status_code == 404
                invalid = await client.put("/v0/album/1", json={"title": "x" * 161, "artistId": 2})
                assert invalid.status_code == 422
                assert invalid.json()["title"] == "Unprocessable Content"
                assert invalid.json()["code"] == "validation_error"
                invalid_patch = await client.patch("/v0/album/1", json={"title": "x" * 161})
                assert invalid_patch.status_code == 422
                assert (await client.get("/v0/album/1")).json() == updated.json()

                missing_delete = await client.delete("/v0/album/999")
                assert missing_delete.status_code == 404
                deleted = await client.delete("/v0/album/1")
                assert deleted.status_code == 204
                assert deleted.content == b""
                assert (await client.get("/v0/album/1")).status_code == 404
                assert (await client.delete("/v0/album/1")).status_code == 404

    asyncio.run(scenario())


def test_app_registration_custom_views_and_direct_adapters() -> None:
    class ItemApi(ModelApi[Item]):
        resource = Items
        actions = ("list", "retrieve")

    class ItemDetail(DetailView):
        resource = Items

        def get_context_data(self, **kwargs: Any) -> Any:
            context = super().get_context_data(**kwargs)
            context["label"] = context["object"].name
            return context

        def render_to_response(self, context: Any, **kwargs: Any) -> Any:
            from fastapi.responses import PlainTextResponse

            return PlainTextResponse(context["label"])

    class ItemPages(ModelViewSet[Item]):
        resource = Items
        slug = "inventory"
        detail_view = ItemDetail

    class Inventory(AppConfig):
        name = "tests"
        label = "tests"
        views: ClassVar[list[Any]] = [ItemPages]
        apis: ClassVar[list[Any]] = [ItemApi]

    async def scenario() -> None:
        async with configured():
            settings.INSTALLED_APPS = [Inventory("tests", import_module("tests"))]
            app = authenticated_app()
            obox = Obox(app)
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
            assert set(schema["/v0/tests/inventory"]) == {"get"}
            assert set(schema["/custom/items"]) == {"get"}
            assert "/v0/inventory" not in schema
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://test",
                    headers={"x-test-user": "admin"},
                ) as client,
            ):
                assert (await client.get("/tests/inventory")).status_code == 200
                assert (await client.get("/v0/tests/inventory")).status_code == 200
                await client.get("/tests/inventory/add")
                assert (
                    await client.post(
                        "/tests/inventory/add",
                        data={
                            "csrfmiddlewaretoken": client.cookies["csrftoken"],
                            "name": "one",
                            "price": "2",
                        },
                    )
                ).status_code == 303
                assert (await client.get("/tests/inventory/1/view")).text == "one"
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
