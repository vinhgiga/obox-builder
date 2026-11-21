import asyncio
import re
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from obox import Obox, Resource
from obox._auth import Principal
from obox.builder import ModelBuilder
from obox.core.exceptions import ImproperlyConfigured

from tests.test_forms_integration import Item, ItemForm, configured


class Actors:
    def __init__(self, app: Any, actor: Any) -> None:
        self.app, self.actor = app, actor

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        scope["user"] = self.actor
        await self.app(scope, receive, send)


def test_builder_registration_uses_explicit_models_and_admin_names() -> None:

    class Items(ModelBuilder):
        form = ItemForm
        fields = ("name", "price", "quantity")
        readonly_fields = ("quantity",)
        list_display = ("id", "name")

    owner = Items(Item)
    assert isinstance(owner.get_queryset(None), sa.Select)
    assert "quantity" not in owner.get_form(None).base_fields

    async def scenario() -> None:
        async with configured():
            app = FastAPI(openapi_url=None)
            obox = Obox(app)
            obox.register(Item, builder=Items)
            with pytest.raises(ImproperlyConfigured, match="already registered"):
                obox.register(Item, api=False)
            assert str(app.url_path_for("obox:views:/items:changelist")) == "/items"

    asyncio.run(scenario())


def test_admin_ui_calls_standard_form_permission_save_and_delete_hooks() -> None:
    events = []

    class Items(ModelBuilder):
        form = ItemForm
        fields = ("name", "price")
        list_display = ("id", "name")
        search_fields = ("name",)
        fieldsets = (("Details", {"fields": ("name", "price")}),)

        async def save_model(self, request: Any, obj: Item, form: Any, change: bool) -> None:
            events.append(("save", change))
            await super().save_model(request, obj, form, change)

    class ItemsResource(Resource[Item]):
        model = Item

        async def can(self, action: str, obj: Item | None, actor: Any) -> bool:
            if action == "delete" and obj is not None:
                events.append(("delete", obj.id))
            return True

    async def scenario() -> None:
        async with configured():
            app = FastAPI(openapi_url=None)
            app.add_middleware(
                Actors,
                actor=Principal(id=1, is_authenticated=True, is_superuser=True),
            )
            Obox(app).register(ItemsResource, builder=Items)
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client,
            ):
                assert (await client.get("/api/tests/items/")).status_code == 404
                page = await client.get("/items/add")
                assert page.status_code == 200
                assert "Details" in page.text
                token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.text)
                assert token is not None
                data = {"csrfmiddlewaretoken": token[1], "name": "Alpha", "price": "1.00"}
                saved = await client.post("/items/add", data=data)
                assert saved.status_code == 303
                assert "alpha" in (await client.get("/items")).text
                assert "alpha" not in (await client.get("/items?q=absent")).text
                assert (
                    await client.post("/items/1/change", data={**data, "name": "Beta"})
                ).status_code == 303
                assert (
                    await client.post("/items/1/delete", data={"csrfmiddlewaretoken": token[1]})
                ).status_code == 303
                assert events == [("save", False), ("save", True), ("delete", 1)]
                assert (await client.get("/items/1/change")).status_code == 404

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


def test_builder_honors_custom_form_fields_and_factory_metadata() -> None:
    class NameForm(ItemForm):
        class Meta(ItemForm.Meta):
            fields = ("name",)

    owner = ModelBuilder(Item)
    owner.form = NameForm
    assert list(owner.get_form(None).base_fields) == ["name"]
    from obox.forms import modelform_factory

    renamed = modelform_factory(Item, ItemForm, labels={"name": "Custom label"})
    assert renamed.base_fields["name"].label == "Custom label"


def test_admin_list_links_pagination_and_action_confirmation() -> None:
    async def scenario() -> None:
        async with configured():
            for name in ("alpha", "beta", "gamma"):
                form = ItemForm({"name": name, "price": "1"})
                assert await form.is_valid()
                await form.save()

            class Items(ModelBuilder):
                fields = ("name", "price")
                list_display = ("name",)
                list_per_page = 2
                search_fields = ("name",)

            app = FastAPI(openapi_url=None)
            app.add_middleware(
                Actors,
                actor=Principal(id=1, is_authenticated=True, is_superuser=True),
            )
            Obox(app).register(Item, builder=Items, api=False)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get("/items")
                assert "alpha" in response.text
                assert "gamma" not in response.text
                assert "/items/1/view" in response.text
                assert 'name="q"' in response.text
                assert "gamma" in (await client.get("/items?p=1")).text
                token = client.cookies["csrftoken"]
                data = {
                    "action": "delete_selected",
                    "_selected_action": "1",
                    "csrfmiddlewaretoken": token,
                }
                confirmation = await client.post("/items", data=data)
                assert confirmation.status_code == 200
                assert 'name="post" value="yes"' in confirmation.text
                assert (
                    await client.post("/items", data={**data, "post": "yes"})
                ).status_code == 303
                assert "alpha" not in (await client.get("/items")).text

    asyncio.run(scenario())
