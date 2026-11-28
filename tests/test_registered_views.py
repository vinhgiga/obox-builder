import asyncio
import json
import re
from importlib import import_module
from typing import Any, ClassVar

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from obox import ModelResource, Obox
from obox.apps import AppConfig
from obox.conf import settings
from obox.core.exceptions import ImproperlyConfigured
from obox.core.principal import ANONYMOUS, Principal
from obox.db.session import session_scope
from obox.runtime.registration import Registration
from obox.views.generic import (
    CreateView,
    DeleteView,
    GridView,
    ModelViewSet,
    UpdateView,
)

from tests.runtime import resource_for
from tests.test_forms_integration import Item, ItemForm, ItemResource, configured


class Actors:
    def __init__(self, app: Any, actor: Any) -> None:
        self.app, self.actor = app, actor

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        scope["user"] = self.actor
        await self.app(scope, receive, send)


def install_views(viewset: type[ModelViewSet[Any]]) -> None:
    class TestConfig(AppConfig):
        views: ClassVar[list[Any]] = [viewset]

    settings.INSTALLED_APPS = [TestConfig("tests", import_module("tests"))]


def test_registration_uses_generic_views_and_preserves_route_names() -> None:
    async def scenario() -> None:
        async with configured():
            app = FastAPI(openapi_url=None)
            obox = Obox(app)
            for options, error in (
                ({"api": False, "views": False}, "at least one"),
                ({"api": GridView}, "ModelApi subclass"),
            ):
                kwargs: dict[str, Any] = options
                with pytest.raises(ImproperlyConfigured, match=error):
                    obox.register(Item, **kwargs)
            with pytest.raises(TypeError):
                obox.register(Item, list_view=GridView)  # type: ignore[call-arg]
            with pytest.raises(TypeError):
                AppConfig("tests", import_module("tests")).register(
                    Item,
                    list_view=GridView,  # type: ignore[call-arg]
                )
            with pytest.raises(TypeError):
                Registration(Item, None, False, True, list_view=GridView)  # type: ignore[call-arg]

            class MissingResource(GridView[Item]):
                pass

            with pytest.raises(ImproperlyConfigured, match="ModelResource"):
                MissingResource.as_view()
            obox.register(Item)
            with pytest.raises(ImproperlyConfigured, match="already registered"):
                obox.register(Item, api=False)
            assert str(app.url_path_for("obox:views:/item:changelist")) == "/item"

    asyncio.run(scenario())


def test_registered_views_call_form_and_resource_hooks() -> None:
    events: list[tuple[str, int | bool]] = []

    class ItemList(GridView):
        resource = resource_for(Item)
        fields = ("id", "name")

    class ItemCreate(CreateView):
        form_class = ItemForm
        fieldsets = (("Details", {"fields": ("name", "price", "quantity")}),)

    class ItemUpdate(UpdateView):
        form_class = ItemForm

    class ItemDelete(DeleteView):
        success_url = "/item"

    class ItemsResource(ModelResource[Item]):
        model = Item
        readonly_fields = ("quantity",)

        async def perform_create(self, data: dict[str, Any]) -> Item:
            events.append(("save", False))
            return await super().perform_create(data)

        async def perform_update(self, obj: Item, data: dict[str, Any]) -> Item:
            events.append(("save", True))
            return await super().perform_update(obj, data)

        async def can(self, action: str, obj: Item | None, actor: Any) -> bool:
            if action == "delete" and obj is not None:
                events.append(("delete", obj.id))
            return True

    class ItemViews(ModelViewSet[Item]):
        resource = ItemsResource
        list_view = ItemList
        create_view = ItemCreate
        update_view = ItemUpdate
        delete_view = ItemDelete

    async def scenario() -> None:
        async with configured():
            app = FastAPI(openapi_url=None)
            app.add_middleware(
                Actors,
                actor=Principal(id=1, is_authenticated=True, is_superuser=True),
            )
            install_views(ItemViews)
            Obox(app)
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client,
            ):
                assert (await client.get("/v0/tests/v01_form_items")).status_code == 404
                page = await client.get("/tests/item/add")
                assert page.status_code == 200
                assert "Details" in page.text
                assert "Title" in page.text
                assert 'name="quantity"' not in page.text
                token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.text)
                assert token is not None
                data = {"csrfmiddlewaretoken": token[1], "name": "Alpha", "price": "1.00"}
                saved = await client.post("/tests/item/add", data=data)
                assert saved.status_code == 303
                assert (await client.get("/tests/item")).status_code == 200
                assert (await client.get("/tests/item?q=absent")).status_code == 200
                assert (
                    await client.post("/tests/item/1/change", data={**data, "name": "Beta"})
                ).status_code == 303
                deleted = await client.post(
                    "/tests/item/1/delete", data={"csrfmiddlewaretoken": token[1]}
                )
                assert (deleted.status_code, deleted.headers["location"]) == (
                    303,
                    "/item",
                )
                assert events == [("save", False), ("save", True), ("delete", 1), ("delete", 1)]
                assert (await client.get("/tests/item/1/change")).status_code == 404

    asyncio.run(scenario())


def test_view_only_actor_sees_readonly_values_and_cannot_change() -> None:
    async def scenario() -> None:
        async with configured():
            async with session_scope() as session:
                await ItemResource(session, ANONYMOUS).create({"name": "original", "price": "1"})
            app = FastAPI(openapi_url=None)
            app.add_middleware(
                Actors,
                actor=Principal(
                    id=1,
                    is_authenticated=True,
                    permissions=frozenset({"tests.view_item"}),
                ),
            )
            Obox(app).register(Item, api=False)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get("/item/1/view")
                assert response.status_code == 200
                assert "original" in response.text
                assert 'name="name"' not in response.text
                assert ">Save<" not in response.text
                token = client.cookies.get("csrftoken", "")
                denied = await client.post(
                    "/item/1/change",
                    data={
                        "name": "changed",
                        "price": "1",
                        "csrfmiddlewaretoken": token,
                    },
                )
                assert denied.status_code == 403
                assert "original" in (await client.get("/item/1/view")).text

    asyncio.run(scenario())


def test_registered_grid_links_and_pagination() -> None:
    async def scenario() -> None:
        async with configured():
            async with session_scope() as session:
                resource = ItemResource(session, ANONYMOUS)
                for name in ("alpha", "beta", "gamma"):
                    await resource.create({"name": name, "price": "1"})

            class Items(GridView[Item]):
                fields = ("name",)
                page_size = 2

            class LockedItems(ModelResource[Item]):
                model = Item

                async def can(self, action: str, obj: Item | None, actor: Any) -> bool:
                    return not (action == "delete" and obj is not None and obj.name == "gamma")

            class LockedItemViews(ModelViewSet[Item]):
                resource = LockedItems
                list_view = Items

            app = FastAPI(openapi_url=None)
            app.add_middleware(
                Actors,
                actor=Principal(id=1, is_authenticated=True, is_superuser=True),
            )
            install_views(LockedItemViews)
            Obox(app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get("/tests/item")
                assert "alpha" in response.text
                assert "gamma" not in response.text
                assert "/tests/item/1/view" in response.text
                assert 'name="q"' in response.text
                assert (await client.post("/tests/item")).status_code == 405
                protected = await client.get("/tests/item/3/view")
                assert "Edit" in protected.text
                assert 'href="/tests/item/3/delete"' not in protected.text
                assert "gamma" in (await client.get("/tests/item?page=2")).text

    asyncio.run(scenario())


def test_grid_shares_filters_search_and_scope_with_rest() -> None:
    from obox.db.filters import FilterGroup, FilterTerm
    from obox.views.generic import ViewConfig

    class VisibleItems(ModelResource[Item]):
        model = Item
        fields = ("name", "price", "quantity")
        search_fields = ("name",)

        def get_queryset(self) -> sa.Select[tuple[Item]]:
            return super().get_queryset().where(Item.name != "private")

    class Items(GridView[Item]):
        fields = ("name",)
        page_size = 2
        view = ViewConfig(filter=FilterGroup(children=(FilterTerm("quantity", "is", 1),)))

    class ItemViews(ModelViewSet[Item]):
        resource = VisibleItems
        list_view = Items

    async def scenario() -> None:
        async with configured():
            async with session_scope() as session:
                resource = ItemResource(session, ANONYMOUS)
                for name, quantity in (("alpha", 1), ("alpine", 2), ("beta", 1), ("private", 1)):
                    await resource.create({"name": name, "price": "1", "quantity": quantity})
            app = FastAPI(openapi_url=None)
            app.add_middleware(
                Actors, actor=Principal(id=1, is_authenticated=True, is_superuser=True)
            )
            Obox(app).register(VisibleItems, slug="inventory", views=ItemViews)
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client,
            ):
                path = "/inventory"
                first = await client.get(path)
                assert "alpha" in first.text
                assert "alpine" not in first.text
                filter_json = json.dumps(
                    FilterGroup(children=(FilterTerm("quantity", "in", [2]),)).to_dict()
                )
                params = {"q": "alp", "filter": filter_json, "ordering": "-name"}
                grid = await client.get(path, params=params)
                assert grid.status_code == 200
                assert "alpine" in grid.text
                assert "alpha" not in grid.text
                response = await client.get("/v0/inventory", params=params)
                assert [row["name"] for row in response.json()["items"]] == ["alpine"]
                cleared = await client.get(
                    path, params={"filter": "", "group": "price", "page": "2"}
                )
                assert cleared.status_code == 200
                assert 'class="group-count">3 records' in cleared.text
                for invalid in (
                    {"filter": "{"},
                    {"quantity__exact": "1"},
                    {"ordering": "unknown"},
                    {"group": "unknown"},
                    {"q": "alpha " * 11},
                ):
                    assert (await client.get(path, params=invalid)).status_code == 422
                assert (
                    await client.get(path, params={"filter": "", "q": "private"})
                ).status_code == 200
                assert "private" not in (await client.get(path, params={"filter": ""})).text

    asyncio.run(scenario())
