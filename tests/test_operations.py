import asyncio
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from obox_builder import API, ModelBuilder, Obox, Principal, View
from obox_builder.auth import StaffOnly
from obox_builder.builder.ui import allowed_operations
from obox_builder.conf import Settings
from obox_builder.context import Context
from obox_builder.errors import ImproperlyConfigured
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


class Widget(Base):
    __tablename__ = "operation_widgets"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(50))


class NoCreateView(View):
    operations = ("create", "update", "delete")

    def get_operations(self, ctx: Context, *, instance: Any = None) -> tuple[str, ...]:
        return ("update", "delete") if instance is not None else ()


class NoCreateBuilder(ModelBuilder[Widget]):
    View = NoCreateView


class ReadOnlyAPI(API):
    operations = ("list", "retrieve")


class ReadOnlyAPIBuilder(ModelBuilder[Widget]):
    API = ReadOnlyAPI


STAFF = Principal(id=1, is_authenticated=True, is_staff=True)


async def make_app(
    builder: type[ModelBuilder[Widget]], *, principal: Principal | None = None
) -> tuple[Obox, Any]:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    app = Obox(
        settings=Settings(
            session_factory=async_sessionmaker(engine, expire_on_commit=False),
            principal_dependency=lambda: principal,
            default_policy=StaffOnly(),
            csrf_secret="x" * 32,
        ),
    )
    app.register(Widget, builder=builder)
    return app, engine


def compile_error(builder: type[ModelBuilder[Widget]]) -> str:
    async def scenario() -> str:
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        app = Obox(
            settings=Settings(
                session_factory=async_sessionmaker(engine, expire_on_commit=False),
                principal_dependency=lambda: None,
                default_policy=StaffOnly(),
                csrf_secret="x" * 32,
            ),
        )
        try:
            with pytest.raises(ImproperlyConfigured) as error:
                app.register(Widget, builder=builder)
            return str(error.value)
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def test_view_get_operations_controls_rendered_operations() -> None:
    async def scenario() -> None:
        app, engine = await make_app(NoCreateBuilder, principal=STAFF)
        builder = app.get_builder(Widget)
        try:
            async with app.uow(principal=STAFF, source="ui") as ctx:
                assert await allowed_operations(builder, ctx) == ()
            async with app.uow(principal=STAFF) as ctx:
                widget_id = (await app.service(Widget).create({"name": "alpha"}, ctx=ctx)).id
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                list_response = await client.get("/admin/widgets")
                detail_response = await client.get(f"/admin/widgets/{widget_id}")
            assert "/admin/widgets/new" not in list_response.text
            assert f"/admin/widgets/{widget_id}/edit" in detail_response.text
            assert f"/admin/widgets/{widget_id}/delete" in detail_response.text
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_api_operations_control_registered_routes() -> None:
    async def scenario() -> None:
        app, engine = await make_app(ReadOnlyAPIBuilder, principal=STAFF)
        builder = app.get_builder(Widget)
        try:
            assert builder.api.operations == ("list", "retrieve")
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                listed = await client.get("/api/widgets")
                created = await client.post("/api/widgets", json={"name": "alpha"})
                updated = await client.patch("/api/widgets/1", json={"name": "beta"})
            assert listed.status_code == 200
            assert created.status_code == 405
            assert updated.status_code == 405
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("surface", "operations"),
    [
        (View, ("create", "list")),
        (API, ("list", "archive")),
        (API, ("list", "list")),
    ],
)
def test_unknown_or_duplicate_operations_are_rejected(
    surface: type[View] | type[API], operations: tuple[str, ...]
) -> None:
    configured = type("Surface", (surface,), {"operations": operations})
    builder = type(
        "TestBuilder",
        (ModelBuilder,),
        {"View" if surface is View else "API": configured},
    )
    assert compile_error(builder) == "Unknown or duplicate surface operations."


def test_actions_surface_api_is_removed() -> None:
    assert not hasattr(View, "actions")
    assert not hasattr(View, "get_actions")
    assert not hasattr(API, "actions")
