import asyncio
from typing import Any

import httpx
import sqlalchemy as sa
from obox_builder import ModelBuilder, Obox, Principal, View
from obox_builder.auth import StaffOnly
from obox_builder.conf import Settings
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


class Widget(Base):
    __tablename__ = "view_widgets"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(50))
    status: Mapped[str] = mapped_column(sa.String(20), default="draft")


class OrderedView(View):
    list_display = ("status", "name")


class OrderedBuilder(ModelBuilder[Widget]):
    View = OrderedView


class DynamicView(View):
    list_display = ("name", "status")

    def get_list_display(self, ctx: Any) -> tuple[str, ...]:
        return ("name",)


class DynamicBuilder(ModelBuilder[Widget]):
    View = DynamicView


class FilterView(View):
    def get_list_filter(self, ctx: Any) -> tuple[str, ...]:
        return ("status",)


class FilterBuilder(ModelBuilder[Widget]):
    View = FilterView
    filter_fields = ("status",)


STAFF = Principal(id=1, is_authenticated=True, is_staff=True)


async def make_app(builder: type[ModelBuilder[Widget]]) -> tuple[Obox, Any]:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    app = Obox(
        settings=Settings(
            session_factory=async_sessionmaker(engine, expire_on_commit=False),
            principal_dependency=lambda: STAFF,
            default_policy=StaffOnly(),
            csrf_secret="x" * 32,
        ),
    )
    app.register(Widget, builder=builder)
    return app, engine


async def get_list(builder: type[ModelBuilder[Widget]]) -> tuple[str, Any]:
    app, engine = await make_app(builder)
    try:
        async with app.uow(principal=STAFF) as ctx:
            await app.service(Widget).create({"name": "alpha", "status": "live"}, ctx=ctx)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/admin/widgets")
        return response.text, app.get_builder(Widget)
    finally:
        await engine.dispose()


def test_list_display_controls_columns_and_order() -> None:
    html, builder = asyncio.run(get_list(OrderedBuilder))
    assert builder.view.list_display == ("status", "name")
    assert builder.view_list_display == ("status", "name")
    assert html.index('<th scope="col">Status</th>') < html.index('<th scope="col">Name</th>')
    assert '<th scope="col">Id</th>' not in html
    assert "live" in html
    assert "alpha" in html


def test_get_list_display_overrides_configured_fields() -> None:
    html, _ = asyncio.run(get_list(DynamicBuilder))
    assert '<th scope="col">Name</th>' in html
    assert '<th scope="col">Status</th>' not in html


def test_get_list_filter_controls_filter_controls() -> None:
    html, _ = asyncio.run(get_list(FilterBuilder))
    assert 'name="filter.status"' in html


def test_legacy_view_column_api_is_removed() -> None:
    assert not hasattr(View, "columns")
    assert not hasattr(View, "get_columns")
    assert not hasattr(View, "get_filters")
