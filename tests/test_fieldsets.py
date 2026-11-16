import asyncio
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from obox._auth import RBAC, Principal
from obox._config import RuntimeConfig
from obox._context import Context
from obox._runtime import _Runtime
from obox.contrib.resources import Resource as ResourceDefinition
from obox.contrib.resources.forms import FormOptions
from obox.contrib.resources.ui import fieldset_groups, form_fields
from obox.core.exceptions import ImproperlyConfigured
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import StaticPool

from tests.helpers import with_builders


class Base(DeclarativeBase):
    pass


class Widget(Base):
    __tablename__ = "fieldset_widgets"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(50))
    status: Mapped[str] = mapped_column(sa.String(20), default="draft")
    note: Mapped[str | None] = mapped_column(sa.String(200), nullable=True)


class WidgetBuilder(ResourceDefinition[Widget]):
    model = Widget
    fields = ("id", "name", "status", "note")


class GroupedForm(FormOptions):
    fieldsets = (
        ("Basics", {"fields": ("name", "status")}),
        ("Details", {"description": "Optional details.", "fields": ("note",)}),
    )


class GroupedBuilder(ResourceDefinition[Widget]):
    model = Widget
    fields = ("id", "name", "status", "note")
    form_options = GroupedForm


class NameOnlyPolicy(RBAC[Widget]):
    def write_fields(self, action: str, ctx: Context, *, obj: Any = None) -> frozenset[str]:
        return frozenset({"name"})


class FilteredBuilder(ResourceDefinition[Widget]):
    model = Widget
    form_options = GroupedForm
    fields = ("name", "status", "note")
    policy = NameOnlyPolicy()


STAFF = Principal(id=1, is_authenticated=True, is_staff=True, is_superuser=True)


async def make_app(
    builder: type[ResourceDefinition[Widget]], *, principal: Principal | None = None
) -> tuple[_Runtime, Any]:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    app = _Runtime(
        FastAPI(),
        settings=with_builders(
            RuntimeConfig(
                session_factory=async_sessionmaker(engine, expire_on_commit=False),
                principal_dependency=lambda: principal,
                default_policy=RBAC(),
                csrf_secret="x" * 32,
            ),
            builder,
        ),
    )
    return (app, engine)


def form_with(fieldsets: Any, fields: Any = None) -> type[FormOptions]:
    return type("TestForm", (FormOptions,), {"fieldsets": fieldsets, "fields": fields})


def compile_error(form: type[FormOptions], **builder_attrs: Any) -> str:

    async def scenario() -> str:
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        builder = type("TestBuilder", (WidgetBuilder,), {"form_options": form, **builder_attrs})
        try:
            with pytest.raises(ImproperlyConfigured) as error:
                _Runtime(
                    FastAPI(),
                    settings=with_builders(
                        RuntimeConfig(
                            session_factory=async_sessionmaker(engine, expire_on_commit=False),
                            principal_dependency=lambda: None,
                            default_policy=RBAC(),
                            csrf_secret="x" * 32,
                        ),
                        builder,
                    ),
                )
            return str(error.value)
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def test_fieldsets_define_form_fields_and_group_metadata() -> None:

    async def scenario() -> None:
        app, engine = await make_app(GroupedBuilder)
        builder = app.get_resource(Widget)
        try:
            assert builder.form_fields == ("name", "status", "note")
            async with app.uow(principal=STAFF, source="ui") as ctx:
                controls = await form_fields(builder, app.service(Widget), ctx, None)
            groups = fieldset_groups(builder, controls)
            assert [
                (group["title"], group["description"], [field["name"] for field in group["fields"]])
                for group in groups
            ] == [("Basics", "", ["name", "status"]), ("Details", "Optional details.", ["note"])]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_fieldsets_drop_policy_hidden_fields_and_empty_groups() -> None:

    async def scenario() -> None:
        app, engine = await make_app(FilteredBuilder)
        builder = app.get_resource(Widget)
        try:
            async with app.uow(principal=STAFF, source="ui") as ctx:
                controls = await form_fields(builder, app.service(Widget), ctx, None)
            groups = fieldset_groups(builder, controls)
            assert [
                (group["title"], [field["name"] for field in group["fields"]]) for group in groups
            ] == [("Basics", ["name"])]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_form_page_renders_fieldsets() -> None:

    async def scenario() -> None:
        app, engine = await make_app(GroupedBuilder, principal=STAFF)
        try:
            transport = httpx.ASGITransport(app=app.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/admin/admin/widgets/new")
            assert response.status_code == 200
            html = response.text
            assert html.count('<fieldset class="form-fieldset">') == 2
            assert '<legend class="form-label">Basics</legend>' in html
            assert html.index('<legend class="form-label">Basics</legend>') < html.index(
                "Optional details."
            )
            assert html.index('name="name"') < html.index('name="status"')
            assert html.index('name="status"') < html.index('name="note"')
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_form_page_without_fieldsets_stays_flat() -> None:

    async def scenario() -> None:
        app, engine = await make_app(WidgetBuilder, principal=STAFF)
        try:
            transport = httpx.ASGITransport(app=app.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/admin/admin/widgets/new")
            assert response.status_code == 200
            assert "<fieldset>" not in response.text
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_empty_fieldsets_fall_back_to_form_fields() -> None:

    async def scenario() -> None:
        builder_type = type("EmptyBuilder", (WidgetBuilder,), {"form_options": form_with(())})
        app, engine = await make_app(builder_type)
        builder = app.get_resource(Widget)
        try:
            assert builder.form_fields == builder.fields
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_fieldsets_and_fields_are_mutually_exclusive() -> None:
    form = form_with((("Basics", {"fields": ("name",)}),), fields=("status",))
    assert compile_error(form) == "Both 'fieldsets' and 'fields' are specified."


@pytest.mark.parametrize(
    ("fieldsets", "message"),
    [
        ("name", "FormOptions.fieldsets must be a list or tuple."),
        ([("Basics",)], "FormOptions.fieldsets[0] must be a (title, options) pair."),
        ([("Basics", {"description": "x"})], "FormOptions.fieldsets[0] must contain 'fields'."),
        (
            [("Basics", {"fields": "name"})],
            "FormOptions.fieldsets[0]['fields'] must be a list or tuple.",
        ),
        (
            [("Basics", {"fields": (("name", "status"),)})],
            "Nested field rows are not supported in fieldsets.",
        ),
        (
            [("Basics", {"fields": ("name",), "classes": ("wide",)})],
            "Unsupported fieldset option: 'classes'.",
        ),
        ([(1, {"fields": ("name",)})], "FormOptions.fieldsets[0] title must be a string or None."),
        (
            [("Basics", {"fields": ("name",), "description": 1})],
            "FormOptions.fieldsets[0] description must be a string.",
        ),
    ],
)
def test_fieldsets_reject_invalid_shape(fieldsets: Any, message: str) -> None:
    assert compile_error(form_with(fieldsets)) == message


@pytest.mark.parametrize(
    ("fieldsets", "builder_attrs", "message"),
    [
        (
            [("A", {"fields": ("name",)}), ("B", {"fields": ("name",)})],
            {},
            "Duplicate field in fieldsets: 'name'.",
        ),
        (
            [("A", {"fields": ("note",)})],
            {"fields": ("name", "status")},
            "FormOptions fieldsets reference unpublished fields.",
        ),
    ],
)
def test_fieldsets_reject_duplicate_or_unpublished_fields(
    fieldsets: Any, builder_attrs: dict[str, Any], message: str
) -> None:
    assert compile_error(form_with(fieldsets), **builder_attrs) == message
