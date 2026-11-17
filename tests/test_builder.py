import asyncio
import re
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from obox import Obox
from obox._auth import Principal
from obox.builder import AdminSite, AlreadyRegistered, ModelBuilder
from obox.urls import _mount, path

from tests.test_forms_integration import Item, ItemForm, configured


class Actors:
    def __init__(self, app: Any, actor: Any) -> None:
        self.app, self.actor = app, actor

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        scope["user"] = self.actor
        await self.app(scope, receive, send)


def test_builder_registration_uses_explicit_models_and_admin_names() -> None:
    site = AdminSite()

    class Items(ModelBuilder):
        form = ItemForm
        fields = ("name", "price", "quantity")
        readonly_fields = ("quantity",)
        list_display = ("id", "name")

    site.register(Item, Items)
    assert site.is_registered(Item)
    assert site._registry[Item].model is Item
    assert isinstance(site._registry[Item].get_queryset(None), sa.Select)
    assert "quantity" not in site._registry[Item].get_form(None).base_fields
    with pytest.raises(AlreadyRegistered):
        site.register(Item)
    site.unregister(Item)
    assert not site.is_registered(Item)


def test_admin_ui_calls_standard_form_permission_save_and_delete_hooks() -> None:
    events = []
    site = AdminSite()

    class Items(ModelBuilder):
        form = ItemForm
        fields = ("name", "price")
        list_display = ("id", "name")
        search_fields = ("name",)
        fieldsets = (("Details", {"fields": ("name", "price")}),)

        async def save_model(self, request: Any, obj: Item, form: Any, change: bool) -> None:
            events.append(("save", change))
            await super().save_model(request, obj, form, change)

        async def delete_model(self, request: Any, obj: Item) -> None:
            events.append(("delete", obj.id))
            await super().delete_model(request, obj)

    site.register(Item, Items)

    async def scenario() -> None:
        async with configured():
            app = FastAPI()
            app.add_middleware(
                Actors,
                actor=Principal(id=1, is_authenticated=True, is_staff=True, is_superuser=True),
            )
            Obox(app)
            _mount(app, [path("manage/", site.urls)])
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client,
            ):
                assert (await client.get("/api/tests/items/")).status_code == 404
                index = await client.get("/manage/")
                assert index.status_code == 200
                page = await client.get("/manage/tests/item/add/")
                assert page.status_code == 200
                assert "Details" in page.text
                token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.text)
                assert token is not None
                data = {"csrfmiddlewaretoken": token[1], "name": "Alpha", "price": "1.00"}
                saved = await client.post("/manage/tests/item/add/", data=data)
                assert saved.status_code == 302
                assert "alpha" in (await client.get("/manage/tests/item/")).text
                assert "alpha" not in (await client.get("/manage/tests/item/?q=absent")).text
                assert (
                    await client.post("/manage/tests/item/1/change/", data={**data, "name": "Beta"})
                ).status_code == 302
                assert (
                    await client.post(
                        "/manage/tests/item/1/delete/", data={"csrfmiddlewaretoken": token[1]}
                    )
                ).status_code == 302
                assert events == [("save", False), ("save", True), ("delete", 1)]
                assert (await client.get("/manage/tests/item/1/change/")).status_code == 404

    asyncio.run(scenario())


def test_view_only_staff_sees_readonly_values_and_cannot_change() -> None:
    async def scenario() -> None:
        async with configured():
            record = ItemForm({"name": "original", "price": "1"})
            assert await record.is_valid()
            await record.save()
            site = AdminSite()
            site.register(Item, fields=("name", "price"))
            app = FastAPI()
            app.add_middleware(
                Actors,
                actor=Principal(
                    id=1,
                    is_authenticated=True,
                    is_staff=True,
                    permissions=frozenset({"tests.view_item"}),
                ),
            )
            Obox(app)
            _mount(app, [path("manage/", site.urls)])
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get("/manage/tests/item/1/change/")
                assert response.status_code == 200
                assert "original" in response.text
                assert 'name="name"' not in response.text
                assert ">Save<" not in response.text
                token = client.cookies["csrftoken"]
                denied = await client.post(
                    "/manage/tests/item/1/change/",
                    data={
                        "name": "changed",
                        "price": "1",
                        "csrfmiddlewaretoken": token,
                    },
                )
                assert denied.status_code == 403
                assert "original" in (await client.get("/manage/tests/item/1/change/")).text

    asyncio.run(scenario())


def test_builder_honors_custom_form_fields_and_factory_metadata() -> None:
    class NameForm(ItemForm):
        class Meta(ItemForm.Meta):
            fields = ("name",)

    owner = ModelBuilder(Item, AdminSite())
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
            site = AdminSite()
            site.register(
                Item,
                fields=("name", "price"),
                list_display=("name",),
                list_per_page=2,
                ordering=("id",),
                search_fields=("name",),
            )
            app = FastAPI()
            app.add_middleware(
                Actors,
                actor=Principal(id=1, is_authenticated=True, is_staff=True, is_superuser=True),
            )
            Obox(app)
            _mount(app, [path("manage/", site.urls)])
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get("/manage/tests/item/")
                assert "alpha" in response.text
                assert "gamma" not in response.text
                assert "/manage/tests/item/1/change/" in response.text
                assert 'name="q"' in response.text
                assert "gamma" in (await client.get("/manage/tests/item/?p=1")).text
                token = client.cookies["csrftoken"]
                data = {
                    "action": "delete_selected",
                    "_selected_action": "1",
                    "csrfmiddlewaretoken": token,
                }
                confirmation = await client.post("/manage/tests/item/", data=data)
                assert confirmation.status_code == 200
                assert 'name="post" value="yes"' in confirmation.text
                assert (
                    await client.post("/manage/tests/item/", data={**data, "post": "yes"})
                ).status_code == 302
                assert "alpha" not in (await client.get("/manage/tests/item/")).text

    asyncio.run(scenario())
