import asyncio
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import HTMLResponse
from obox._auth import RBAC, Principal
from obox._config import RuntimeConfig
from obox._context import Context
from obox._exceptions import BuilderValidationError, NotAuthenticated, PermissionDenied
from obox._runtime import _Runtime
from obox.contrib.resources import Resource as ResourceDefinition
from obox.contrib.resources.apis import ApiOptions
from obox.contrib.resources.forms import FormOptions
from obox.contrib.resources.views import ViewOptions
from obox.core.exceptions import ImproperlyConfigured
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from tests.helpers import with_builders


class Base(DeclarativeBase):
    pass


class Item(Base):
    __tablename__ = "surface_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(20))
    quantity: Mapped[int] = mapped_column(default=1)
    note: Mapped[str | None] = mapped_column(sa.String(40))
    payload: Mapped[Any | None] = mapped_column(sa.JSON)
    internal: Mapped[str] = mapped_column(default="private")


class ItemForm(FormOptions):
    fields: Sequence[str] = ("name", "quantity", "note", "payload")

    async def validate(
        self, data: Mapping[str, Any], ctx: Context, *, instance: Any = None
    ) -> Mapping[str, Any]:
        return {**data, "name": data["name"].strip()} if "name" in data else data


class ItemApi(ApiOptions):
    read_fields = ("id", "name", "quantity", "note", "payload")
    write_fields = ("name", "quantity", "note", "payload")


class ItemBuilder(ResourceDefinition[Item]):
    model = Item
    fields = ("id", "name", "quantity", "note", "payload", "internal")
    filter_fields = ("name",)
    form_options = ItemForm
    api_options = ItemApi


STAFF = Principal(id=1, is_authenticated=True, is_staff=True, is_superuser=True)


def make_app(builder: type[ResourceDefinition[Item]] = ItemBuilder) -> _Runtime:
    return _Runtime(
        FastAPI(),
        settings=with_builders(
            RuntimeConfig(
                database_url="sqlite+aiosqlite://",
                principal_dependency=lambda: STAFF,
                default_policy=RBAC(),
                csrf_secret="x" * 32,
            ),
            builder,
        ),
    )


@asynccontextmanager
async def database_app(
    builder: type[ResourceDefinition[Item]] = ItemBuilder,
) -> AsyncIterator[_Runtime]:
    app = make_app(builder)
    async with app.app.router.lifespan_context(app.app):
        assert app.database.engine is not None
        async with app.database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield app


def test_form_cleans_without_service_http_or_database() -> None:

    async def scenario() -> None:
        builder = make_app().get_resource(Item)
        form = ItemForm(builder=builder)
        async with AsyncSession() as session:
            ctx = Context(principal=STAFF, session=session)
            values = await form.clean(
                {"name": " alpha ", "quantity": "2", "note": "", "payload": '{"ok": true}'}, ctx
            )
            assert values == {"name": "alpha", "quantity": 2, "note": None, "payload": {"ok": True}}
            assert await form.clean({"name": "alpha", "quantity": ""}, ctx) == {"name": "alpha"}
            assert await form.clean({}, ctx, instance=Item(id=1, name="old")) == {}
            assert form.get_initial_values(ctx, ("quantity", "name")) == {"quantity": 1}
            assert not session.new

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("data", "error", "field"),
    [
        ({}, BuilderValidationError, "name"),
        ({"name": "a", "quantity": "bad"}, BuilderValidationError, "quantity"),
        ({"name": "a", "payload": "{"}, BuilderValidationError, "payload"),
        ({"name": "a", "unknown": 1}, BuilderValidationError, "unknown"),
        ({"name": "a", "internal": "exposed"}, PermissionDenied, "internal"),
    ],
)
def test_form_rejects_invalid_or_unpublished_input(
    data: dict[str, Any], error: type[Exception], field: str
) -> None:

    async def scenario() -> None:
        form = make_app().get_resource(Item).form
        async with AsyncSession() as session:
            with pytest.raises(error) as caught:
                await form.clean(data, Context(principal=STAFF, session=session))
            if isinstance(caught.value, BuilderValidationError):
                assert field in caught.value.errors
            else:
                assert field in str(caught.value)

    asyncio.run(scenario())


def test_form_save_and_api_crud_are_independent_of_request_source() -> None:

    async def scenario() -> None:
        async with database_app() as app:
            builder = app.get_resource(Item)
            async with app.uow(principal=STAFF) as ctx:
                created = await builder.form.save({"name": " form ", "payload": '"text"'}, ctx)
                assert (created.name, created.payload) == ("form", "text")
                updated = await builder.form.save({"quantity": "3"}, ctx, instance=created)
                assert updated is created
                assert updated.quantity == 3
                api_record = await builder.api.create({"name": " api "}, ctx)
                assert api_record.name == " api "
                assert await builder.api.serialize(api_record, ctx, fields=("id", "internal")) == {
                    "id": str(api_record.id)
                }
                assert await builder.api.retrieve(api_record.id, ctx) is api_record
                await builder.api.update(api_record.id, {"quantity": 4}, ctx)
                page = await builder.api.list([("select", "name,quantity")], ctx)
                assert page["items"] == [
                    {"id": str(created.id), "name": "form", "quantity": 3},
                    {"id": str(api_record.id), "name": " api ", "quantity": 4},
                ]
                assert await builder.view.get_object(created.id, ctx) is created
                assert len(await builder.view.get_queryset(ctx)) == 2
                await builder.api.delete(api_record.id, ctx)
                await builder.view.delete(created.id, ctx)
            async with app.uow(principal=STAFF) as ctx:
                assert await builder.view.get_queryset(ctx) == []

    asyncio.run(scenario())


def test_form_save_preserves_domain_defaults_and_server_presets() -> None:

    class PresetPolicy(RBAC[Item]):
        def presets(self, action: str, ctx: Context) -> Mapping[str, Any]:
            return {"quantity": 7}

    class DefaultBuilder(ItemBuilder):
        policy = PresetPolicy()

        async def validate_create(self, data: dict[str, Any], ctx: Context) -> Mapping[str, Any]:
            return {"name": "default", **data}

    async def scenario() -> None:
        async with database_app(DefaultBuilder) as app:
            form = app.get_resource(Item).form
            async with app.uow(principal=STAFF) as ctx:
                record = await form.save({}, ctx)
                assert (record.name, record.quantity) == ("default", 7)
                with pytest.raises(PermissionDenied, match="quantity"):
                    await form.clean({"name": "a", "quantity": 9}, ctx)

    asyncio.run(scenario())


@pytest.mark.parametrize("hook", ["validate", "clean"])
def test_custom_form_hooks_cannot_add_disallowed_fields(hook: str) -> None:

    async def inject(self: Any, data: Mapping[str, Any], ctx: Context, **kwargs: Any) -> Any:
        return {**data, "internal": "injected"}

    form_options = type("InjectingForm", (ItemForm,), {hook: inject})
    builder_class = type("InjectingBuilder", (ItemBuilder,), {"form_options": form_options})

    async def scenario() -> None:
        async with database_app(builder_class) as app, app.uow(principal=STAFF) as ctx:
            with pytest.raises(PermissionDenied, match="internal"):
                await app.get_resource(Item).form.save({"name": "a"}, ctx)
            assert not ctx.session.new

    asyncio.run(scenario())


def test_form_and_api_keep_field_permissions_and_preconditions() -> None:

    async def reject(instance: Any) -> None:
        raise PermissionDenied("precondition rejected")

    async def scenario() -> None:
        async with database_app() as app:
            builder = app.get_resource(Item)
            async with app.uow(principal=STAFF) as ctx:
                record = await app.service(Item).create({"name": "old"}, ctx=ctx)
                with pytest.raises(PermissionDenied, match="internal"):
                    await builder.form.save({"internal": "bad"}, ctx, instance=record)
                with pytest.raises(PermissionDenied, match="internal"):
                    await builder.api.update(record.id, {"internal": "bad"}, ctx)
                with pytest.raises(PermissionDenied, match="precondition rejected"):
                    await builder.form.save(
                        {"name": "changed"}, ctx, instance=record, precondition=reject
                    )
                with pytest.raises(PermissionDenied, match="precondition rejected"):
                    await builder.api.update(
                        record.id, {"name": "changed"}, ctx, precondition=reject
                    )
                assert record.name == "old"
                with pytest.raises(BuilderValidationError):
                    await builder.api.list([("select", "internal")], ctx)
            with pytest.raises(PermissionDenied, match="precondition rejected"):
                async with app.uow(principal=STAFF) as ctx:
                    await builder.api.delete(record.id, ctx, precondition=reject)
            async with app.uow(principal=Principal()) as ctx:
                with pytest.raises(NotAuthenticated):
                    await builder.form.save({"name": "denied"}, ctx)

    asyncio.run(scenario())


def test_http_uses_custom_form_view_and_api_methods() -> None:

    class CustomForm(ItemForm):
        async def clean(
            self, data: Mapping[str, Any], ctx: Context, **kwargs: Any
        ) -> dict[str, Any]:
            values = await super().clean(data, ctx, **kwargs)
            if "name" in values:
                values["name"] = "form-" + values["name"]
            return values

    class CustomView(ViewOptions):
        async def get_queryset(self, ctx: Context, **options: Any) -> Sequence[Any]:
            return await super().get_queryset(ctx, **{**options, "filters": {"name": "form-ui"}})

        def render(
            self, template: str, request: Request, ctx: Context, **kwargs: Any
        ) -> HTMLResponse:
            response = super().render(template, request, ctx, **kwargs)
            response.headers["X-Custom-View"] = "yes"
            return response

    class CustomApi(ItemApi):
        async def create(self, data: Mapping[str, Any], ctx: Context) -> Any:
            return await super().create({**data, "name": "api-" + data["name"]}, ctx)

        async def serialize(
            self, instance: Any, ctx: Context, *, fields: Sequence[str] | None = None
        ) -> dict[str, Any]:
            row = await super().serialize(instance, ctx, fields=fields)
            if "name" in row:
                row["name"] = row["name"].upper()
            return row

        def build_router(self) -> APIRouter:
            router = super().build_router()

            @router.get(self.builder.api_path + ":custom")
            async def custom() -> dict[str, bool]:
                return {"custom": True}

            return router

    class CustomBuilder(ItemBuilder):
        form_options = CustomForm
        view_options = CustomView
        api_options = CustomApi

    async def scenario() -> None:
        async with (
            database_app(CustomBuilder) as app,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app.app), base_url="http://test"
            ) as client,
        ):
            page = await client.get("/admin/admin/items/new")
            assert page.headers["X-Custom-View"] == "yes"
            token = re.search('name="csrf_token" value="([^"]+)"', page.text)
            assert token is not None
            saved = await client.post(
                "/admin/admin/items/new", data={"csrf_token": token[1], "name": " ui "}
            )
            assert saved.status_code == 303
            created = await client.post("/api/admin/items", json={"name": "other"})
            assert created.status_code == 201
            assert created.json()["name"] == "API-OTHER"
            listed = await client.get("/admin/admin/items")
            assert "form-ui" in listed.text
            assert "api-other" not in listed.text
            assert (await client.get("/api/admin/items:custom")).json() == {"custom": True}
            schema = (await client.get("/openapi.json")).json()
            assert "post" in schema["paths"]["/api/admin/items"]

    asyncio.run(scenario())


def test_explicit_presentations_inherit_and_bind_per_app() -> None:

    class LegacyBuilder(ItemBuilder):
        class FormOptions(ItemForm):
            fields = ("name",)

        form_options = FormOptions

    class ModernBuilder(LegacyBuilder):
        form_options = ItemForm

    for cls, expected in ((LegacyBuilder, LegacyBuilder.form_options), (ModernBuilder, ItemForm)):
        first, second = (make_app(cls).get_resource(Item) for _ in range(2))
        assert type(first.form) is expected
        assert first.form_options is expected
        for name in ("form", "view", "api"):
            one, two = (getattr(first, name), getattr(second, name))
            assert one is not two
            assert one.builder is first
            assert two.builder is second
            assert one.service is first.app.service(Item)
            assert two.service is second.app.service(Item)


@pytest.mark.parametrize("name", ["form_options", "view_options", "api_options"])
def test_invalid_surface_classes_fail_during_registration(name: str) -> None:
    invalid = type("InvalidBuilder", (ItemBuilder,), {name: object})
    with pytest.raises(ImproperlyConfigured, match=name):
        make_app(invalid)


@pytest.mark.parametrize("surface", [FormOptions, ViewOptions, ApiOptions])
def test_unbound_surfaces_report_missing_binding(surface: type[Any]) -> None:
    instance = surface()
    for attribute in ("builder", "service"):
        with pytest.raises(ImproperlyConfigured, match=attribute):
            getattr(instance, attribute)
