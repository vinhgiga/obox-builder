import asyncio
import json
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


class GroupedView(View):
    list_display = ("status", "name")
    page_size = 2


class GroupedBuilder(ModelBuilder[Widget]):
    View = GroupedView


class SearchBuilder(ModelBuilder[Widget]):
    search_fields = ("name",)


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
    assert html.index('data-list-column="status"') < html.index('data-list-column="name"')
    assert 'data-list-column="id"' not in html
    assert "live" in html
    assert "alpha" in html


def test_list_exposes_row_height_controls() -> None:
    html, _ = asyncio.run(get_list(OrderedBuilder))
    assert 'data-row-height="medium"' in html
    assert 'data-wrap-headers="false"' in html
    for value in ("compact", "medium", "tall", "extra-tall"):
        assert f'data-row-height-choice="{value}"' in html
    for label in ("Short", "Medium", "Tall", "Extra Tall", "Wrap headers"):
        assert label in html
    assert '<div class="cell-value">live</div>' in html


def test_list_exposes_global_search_controls() -> None:
    html, _ = asyncio.run(get_list(SearchBuilder))
    for marker in (
        "data-search-toggle",
        "data-search-panel",
        "data-search-input",
        "data-search-prev",
        "data-search-next",
        "data-search-close",
    ):
        assert marker in html
    assert 'aria-controls="grid-search"' in html
    assert 'name="search"' not in html


def test_get_list_display_overrides_configured_fields() -> None:
    html, _ = asyncio.run(get_list(DynamicBuilder))
    assert 'data-list-column="name">Name</th>' in html
    assert 'data-list-column="status"' not in html


def test_get_list_filter_controls_filter_controls() -> None:
    html, _ = asyncio.run(get_list(FilterBuilder))
    assert "data-filter-input" in html


def test_list_filter_groups_match_nested_conditions() -> None:
    async def run() -> None:
        app, engine = await make_app(FilterBuilder)
        try:
            async with app.uow(principal=STAFF) as ctx:
                service = app.service(Widget)
                for name, status in (("alpha", "live"), ("beta", "draft"), ("gamma", "")):
                    await service.create({"name": name, "status": status}, ctx=ctx)
            tree = {
                "conjunction": "and",
                "children": [
                    {"field": "status", "op": "is_not_empty", "value": ""},
                    {
                        "conjunction": "or",
                        "children": [
                            {"field": "status", "op": "is", "value": "live"},
                            {"field": "status", "op": "is", "value": "draft"},
                        ],
                    },
                ],
            }
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/api/widgets", params={"filter": json.dumps(tree)})
                page = await client.get("/admin/widgets", params={"filter": json.dumps(tree)})
                html = page.text
            assert {row["name"] for row in response.json()} == {"alpha", "beta"}
            assert "alpha" in html and "beta" in html and "gamma" not in html
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_list_sorts_by_multiple_fields_and_exposes_sort_config() -> None:
    async def run() -> None:
        app, engine = await make_app(OrderedBuilder)
        try:
            async with app.uow(principal=STAFF) as ctx:
                for name, status in (("beta", "live"), ("alpha", "live"), ("gamma", "draft")):
                    await app.service(Widget).create({"name": name, "status": status}, ctx=ctx)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/admin/widgets", params={"ordering": "status,-name"})
            html = response.text
            assert html.index("gamma") < html.index("beta") < html.index("alpha")
            config = json.loads(html.split("data-list-sort-config>")[1].split("</script>")[0])
            assert config["sort"] == [
                {"field": "status", "descending": False},
                {"field": "name", "descending": True},
            ]
            assert {item["name"]: item["kind"] for item in config["fields"]} == {
                "id": "number",
                "name": "text",
                "status": "text",
            }
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_list_groups_nested_rows_with_cross_page_counts() -> None:
    async def run() -> None:
        app, engine = await make_app(GroupedBuilder)
        try:
            async with app.uow(principal=STAFF) as ctx:
                for name, status in (
                    ("alpha", "live"),
                    ("beta", "live"),
                    ("gamma", "live"),
                    ("delta", "draft"),
                    ("epsilon", "draft"),
                ):
                    await app.service(Widget).create({"name": name, "status": status}, ctx=ctx)
                counts = await app.service(Widget).group_counts(
                    ctx=ctx,
                    grouping=("status", "name"),
                    prefixes=[("draft",), ("draft", "delta")],
                )
                assert counts == {("draft",): 2, ("draft", "delta"): 1}
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                first = await client.get("/admin/widgets", params={"group": "status,name"})
                second = await client.get("/admin/widgets", params={"group": "status", "offset": 2})
                invalid = await client.get("/admin/widgets", params={"group": "missing"})
            html = first.text
            assert 'data-group-header="1"' in html
            assert 'data-group-header="2"' in html
            assert ">Status<" in html and ">draft<" in html
            assert '<span class="group-count">2</span>' in html
            config = json.loads(html.split("data-list-group-config>")[1].split("</script>")[0])
            assert config["group"] == [
                {"field": "status", "descending": False},
                {"field": "name", "descending": False},
            ]
            assert ">live<" in second.text
            assert '<span class="group-count">3</span>' in second.text
            assert invalid.status_code == 422
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_list_filter_rejects_invalid_groups() -> None:
    async def run() -> None:
        app, engine = await make_app(FilterBuilder)
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                for raw in (
                    "{",
                    json.dumps(
                        {
                            "conjunction": "and",
                            "children": [{"field": "status", "op": "bogus", "value": "live"}],
                        }
                    ),
                ):
                    response = await client.get("/admin/widgets", params={"filter": raw})
                    assert response.status_code == 422
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_legacy_view_column_api_is_removed() -> None:
    assert not hasattr(View, "columns")
    assert not hasattr(View, "get_columns")
    assert not hasattr(View, "get_filters")
