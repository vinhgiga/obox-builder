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
from obox.contrib.resources.apis import ApiOptions
from obox.contrib.resources.ui import allowed_operations
from obox.contrib.resources.views import ViewOptions
from obox.core.exceptions import ImproperlyConfigured
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import StaticPool

from tests.helpers import with_builders


class Base(DeclarativeBase):
    pass


class Widget(Base):
    __tablename__ = "operation_widgets"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(50))


class WidgetBuilder(ResourceDefinition[Widget]):
    model = Widget
    fields = ("id", "name")


class NoCreateView(ViewOptions):
    operations = ("create", "update", "delete")

    def get_operations(self, ctx: Context, *, instance: Any = None) -> tuple[str, ...]:
        return ("update", "delete") if instance is not None else ()


class NoCreateBuilder(ResourceDefinition[Widget]):
    model = Widget
    fields = ("id", "name")
    view_options = NoCreateView


class ReadOnlyAPI(ApiOptions):
    operations = ("list", "retrieve")


class ReadOnlyAPIBuilder(ResourceDefinition[Widget]):
    model = Widget
    fields = ("id", "name")
    api_options = ReadOnlyAPI


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


def compile_error(builder: type[ResourceDefinition[Widget]]) -> str:

    async def scenario() -> str:
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
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


def test_view_get_operations_controls_rendered_operations() -> None:

    async def scenario() -> None:
        app, engine = await make_app(NoCreateBuilder, principal=STAFF)
        builder = app.get_resource(Widget)
        try:
            async with app.uow(principal=STAFF, source="ui") as ctx:
                assert await allowed_operations(builder, ctx) == ()
            async with app.uow(principal=STAFF) as ctx:
                widget_id = (await app.service(Widget).create({"name": "alpha"}, ctx=ctx)).id
            transport = httpx.ASGITransport(app=app.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                list_response = await client.get("/admin/admin/widgets")
                detail_response = await client.get(f"/admin/admin/widgets/{widget_id}")
            assert "/admin/admin/widgets/new" not in list_response.text
            for name in ("_save", "_addanother", "_continue"):
                assert f'name="{name}"' in detail_response.text
            assert f"/admin/admin/widgets/{widget_id}/delete" in detail_response.text
            assert 'class="btn btn-danger"' in detail_response.text
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_api_operations_control_registered_routes() -> None:

    async def scenario() -> None:
        app, engine = await make_app(ReadOnlyAPIBuilder, principal=STAFF)
        builder = app.get_resource(Widget)
        try:
            assert builder.api.operations == ("list", "retrieve")
            transport = httpx.ASGITransport(app=app.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                listed = await client.get("/api/admin/widgets")
                created = await client.post("/api/admin/widgets", json={"name": "alpha"})
                updated = await client.patch("/api/admin/widgets/1", json={"name": "beta"})
            assert listed.status_code == 200
            assert created.status_code == 405
            assert updated.status_code == 405
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("surface", "operations"),
    [
        (ViewOptions, ("create", "list")),
        (ApiOptions, ("list", "archive")),
        (ApiOptions, ("list", "list")),
    ],
)
def test_unknown_or_duplicate_operations_are_rejected(
    surface: type[ViewOptions] | type[ApiOptions], operations: tuple[str, ...]
) -> None:
    configured = type("Surface", (surface,), {"operations": operations})
    builder = type(
        "TestBuilder",
        (WidgetBuilder,),
        {"view_options" if surface is ViewOptions else "api_options": configured},
    )
    assert compile_error(builder) == "Unknown or duplicate surface operations."


def test_actions_surface_api_is_removed() -> None:
    assert not hasattr(ViewOptions, "actions")
    assert not hasattr(ViewOptions, "get_actions")
    assert not hasattr(ApiOptions, "actions")
