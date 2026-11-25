import asyncio
import re
from importlib import import_module
from typing import Any, ClassVar

import httpx
import pytest
import sqlalchemy as sa
from fastapi import Depends, FastAPI
from obox import Obox, Resource, permission_required
from obox.apps import AppConfig
from obox.conf import settings
from obox.core.exceptions import ImproperlyConfigured
from obox.core.principal import Principal
from obox.db.session import session_scope
from obox.views.generic import CreateView, DeleteView, ListView, ModelViewSet, UpdateView, action

from tests.test_forms_integration import Item, ItemForm, configured


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
                ({"api": ListView}, "ModelApi subclass"),
            ):
                kwargs: dict[str, Any] = options
                with pytest.raises(ImproperlyConfigured, match=error):
                    obox.register(Item, **kwargs)
            with pytest.raises(TypeError):
                obox.register(Item, list_view=ListView)  # type: ignore[call-arg]
            with pytest.raises(TypeError):
                AppConfig("tests", import_module("tests")).register(
                    Item,
                    list_view=ListView,  # type: ignore[call-arg]
                )
            obox.register(Item)
            with pytest.raises(ImproperlyConfigured, match="already registered"):
                obox.register(Item, api=False)
            assert str(app.url_path_for("obox:views:/items:changelist")) == "/items"

    asyncio.run(scenario())


def test_registered_views_call_form_and_resource_hooks() -> None:
    events: list[tuple[str, int | bool]] = []

    class ItemList(ListView):
        list_display = ("id", "name")
        search_fields = ("name",)

    class RecordSave:
        async def save_model(self, request: Any, obj: Item, form: Any, change: bool) -> None:
            events.append(("save", change))

    class ItemCreate(RecordSave, CreateView):
        form_class = ItemForm
        fieldsets = (("Details", {"fields": ("name", "price", "quantity")}),)

    class ItemUpdate(RecordSave, UpdateView):
        form_class = ItemForm

    class ItemDelete(DeleteView):
        success_url = "/items"

    class ItemsResource(Resource[Item]):
        model = Item
        readonly_fields = ("quantity",)

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
                assert (await client.get("/api/tests/items/")).status_code == 404
                page = await client.get("/tests/items/add")
                assert page.status_code == 200
                assert "Details" in page.text
                assert 'name="quantity"' not in page.text
                token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.text)
                assert token is not None
                data = {"csrfmiddlewaretoken": token[1], "name": "Alpha", "price": "1.00"}
                saved = await client.post("/tests/items/add", data=data)
                assert saved.status_code == 303
                assert "alpha" in (await client.get("/tests/items")).text
                assert "alpha" not in (await client.get("/tests/items?q=absent")).text
                assert (
                    await client.post("/tests/items/1/change", data={**data, "name": "Beta"})
                ).status_code == 303
                deleted = await client.post(
                    "/tests/items/1/delete", data={"csrfmiddlewaretoken": token[1]}
                )
                assert (deleted.status_code, deleted.headers["location"]) == (
                    303,
                    "/items",
                )
                assert events == [("save", False), ("save", True), ("delete", 1), ("delete", 1)]
                assert (await client.get("/tests/items/1/change")).status_code == 404

    asyncio.run(scenario())


def test_view_only_actor_sees_readonly_values_and_cannot_change() -> None:
    async def scenario() -> None:
        async with configured():
            record = ItemForm({"name": "original", "price": "1"})
            assert await record.is_valid()
            await record.save()
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
                response = await client.get("/items/1/view")
                assert response.status_code == 200
                assert "original" in response.text
                assert 'name="name"' not in response.text
                assert ">Save<" not in response.text
                token = client.cookies.get("csrftoken", "")
                denied = await client.post(
                    "/items/1/change",
                    data={
                        "name": "changed",
                        "price": "1",
                        "csrfmiddlewaretoken": token,
                    },
                )
                assert denied.status_code == 403
                assert "original" in (await client.get("/items/1/view")).text

    asyncio.run(scenario())


def test_create_view_honors_custom_form_and_factory_metadata() -> None:
    class NameForm(ItemForm):
        class Meta:
            model = Item
            fields = ("name",)

    view = CreateView(model=Item, form_class=NameForm)
    assert list(view.get_form_class().base_fields) == ["name"]
    from obox.forms import modelform_factory

    renamed = modelform_factory(Item, ItemForm, labels={"name": "Custom label"})
    assert renamed.base_fields["name"].label == "Custom label"


def test_registered_list_links_pagination_and_action_confirmation() -> None:
    async def scenario() -> None:
        async with configured():
            for name in ("alpha", "beta", "gamma"):
                form = ItemForm({"name": name, "price": "1"})
                assert await form.is_valid()
                await form.save()

            class Items(ListView):
                list_display = ("name",)
                paginate_by = 2
                search_fields = ("name",)

            class LockedItems(Resource[Item]):
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
                response = await client.get("/tests/items")
                assert "alpha" in response.text
                assert "gamma" not in response.text
                assert "/tests/items/1/view" in response.text
                assert 'name="q"' in response.text
                assert "gamma" in (await client.get("/tests/items?p=1")).text
                token = client.cookies["csrftoken"]
                data = {
                    "action": "delete_selected",
                    "_selected_action": "1",
                    "csrfmiddlewaretoken": token,
                }
                confirmation = await client.post("/tests/items", data=data)
                assert confirmation.status_code == 200
                assert 'name="post" value="yes"' in confirmation.text
                denied = await client.post(
                    "/tests/items",
                    data={**data, "_selected_action": ["1", "3"], "post": "yes"},
                )
                assert denied.status_code == 403
                assert "alpha" in (await client.get("/tests/items")).text
                assert (
                    await client.post("/tests/items", data={**data, "post": "yes"})
                ).status_code == 303
                assert "alpha" not in (await client.get("/tests/items")).text

    asyncio.run(scenario())


def test_same_list_view_supports_filters_search_and_actions_on_both_routes() -> None:
    seen = []

    class VisibleItems(Resource[Item]):
        model = Item

        def get_queryset(self) -> sa.Select[tuple[Item]]:
            return super().get_queryset().where(Item.name != "private")

    class Items(ListView):
        list_display = ("name",)
        list_filter = ("quantity",)
        search_fields = ("name",)
        paginate_by = 2
        actions = ("select_rows",)

        @action(permissions=("change",), description="Select rows")
        async def select_rows(self, request: Any, queryset: Any) -> None:
            async with session_scope() as session:
                seen.append([item.name for item in await session.scalars(queryset)])

    class ItemViews(ModelViewSet[Item]):
        resource = VisibleItems
        list_view = Items

    async def scenario() -> None:
        async with configured():
            ids = []
            for name, quantity in (("alpha", 1), ("alpine", 2), ("beta", 1), ("private", 1)):
                form = ItemForm({"name": name, "price": "1", "quantity": str(quantity)})
                assert await form.is_valid()
                ids.append(str((await form.save()).id))

            app = FastAPI(openapi_url=None)
            app.add_middleware(
                Actors,
                actor=Principal(
                    id=1,
                    is_authenticated=True,
                    permissions=frozenset({"tests.view_item", "tests.change_item"}),
                ),
            )
            install_views(ItemViews)
            Obox(app)
            app.add_api_route(
                "/inventory",
                Items.as_view(resource=VisibleItems),
                methods=["GET", "POST"],
                dependencies=[Depends(permission_required("tests.view_item", html=True))],
            )
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client,
            ):
                for path, page in (("/tests/items", "p=1"), ("/inventory", "page=2")):
                    first = await client.get(path)
                    assert "alpha" in first.text
                    assert "beta" not in first.text
                    assert "beta" in (await client.get(f"{path}?{page}")).text
                    filtered = f"{path}?q=alp&quantity__exact=1"
                    result = await client.get(filtered)
                    assert result.status_code == 200
                    assert "alpha" in result.text
                    assert all(name not in result.text for name in ("alpine", "beta", "private"))
                    assert result.text.count("Select rows") == 1
                    assert "delete_selected" not in result.text
                    data = {"action": "select_rows", "_selected_action": ids}
                    assert (await client.post(filtered, data=data)).status_code == 403
                    data["csrfmiddlewaretoken"] = client.cookies["csrftoken"]
                    assert (await client.post(filtered, data=data)).status_code == 303
                assert seen == [["alpha"], ["alpha"]]

    asyncio.run(scenario())
