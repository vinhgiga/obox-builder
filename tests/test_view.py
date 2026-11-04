import asyncio
import json
import re
from typing import Any

import httpx
import sqlalchemy as sa
from fastapi import FastAPI
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
        FastAPI(),
        settings=Settings(
            session_factory=async_sessionmaker(engine, expire_on_commit=False),
            principal_dependency=lambda: STAFF,
            default_policy=StaffOnly(),
            csrf_secret="x" * 32,
        ),
    )
    app.register(builder)
    return app, engine


async def get_list(builder: type[ModelBuilder[Widget]]) -> tuple[str, Any]:
    app, engine = await make_app(builder)
    try:
        async with app.uow(principal=STAFF) as ctx:
            await app.service(Widget).create({"name": "alpha", "status": "live"}, ctx=ctx)
        transport = httpx.ASGITransport(app=app.app)
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
    assert 'class="table table-vcenter record-grid"' in html


def test_list_exposes_bulk_delete_controls() -> None:
    html, _ = asyncio.run(get_list(OrderedBuilder))
    assert 'class="page grid-layout"' in html
    assert re.search(r'action="/en/admin/widgets/bulk-actions"', html)
    assert "data-select-all" in html
    assert 'name="record_ids"' in html
    assert "data-selection-status" in html
    assert "data-bulk-controls hidden" in html
    assert html.index("data-bulk-controls") < html.index('class="grid-tools"')
    assert "data-select-across" in html
    assert "data-clear-selection" in html
    assert "Delete selected" in html
    assert "Run" in html


def test_list_renders_create_action_above_footer() -> None:
    html, _ = asyncio.run(get_list(OrderedBuilder))
    assert re.search(r'<a class="grid-add" href="/en/admin/widgets/new">', html)
    assert "Add record" in html
    assert (
        html.index('class="grid-scroll"')
        < html.index('class="grid-add"')
        < html.index('class="grid-footer"')
    )


def test_bulk_delete_validates_and_deletes_selected_records() -> None:
    async def scenario() -> None:
        app, engine = await make_app(FilterBuilder)
        try:
            async with app.uow(principal=STAFF) as ctx:
                service = app.service(Widget)
                first = await service.create({"name": "alpha", "status": "live"}, ctx=ctx)
                await service.create({"name": "beta", "status": "draft"}, ctx=ctx)
                third = await service.create({"name": "gamma", "status": "live"}, ctx=ctx)
                await service.create({"name": "delta", "status": "draft"}, ctx=ctx)

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app.app), base_url="http://test"
            ) as client:
                page = await client.get("/admin/widgets?filter.status=live&limit=1&offset=0")
                token = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
                assert token is not None
                assert 'data-total-count="2"' in page.text
                data = {
                    "csrf_token": token.group(1),
                    "action": "delete",
                    "record_ids": [str(first.id), str(first.id)],
                    "select_across": "0",
                }
                duplicate = await client.post("/admin/widgets/bulk-actions", data=data)
                assert duplicate.status_code == 422

                data["record_ids"] = [str(first.id)]
                deleted = await client.post("/admin/widgets/bulk-actions", data=data)
                assert deleted.status_code == 303
                assert deleted.headers["location"] == "/en/admin/widgets"

                data.update(record_ids=[str(third.id)], select_across="1")
                across = await client.post(
                    "/admin/widgets/bulk-actions?filter.status=live&limit=1&offset=0",
                    data=data,
                )
                assert across.status_code == 303
                assert across.headers["location"] == (
                    "/en/admin/widgets?filter.status=live&limit=1&offset=0"
                )

            async with app.uow(principal=STAFF) as ctx:
                remaining = list((await ctx.session.scalars(sa.select(Widget))).all())
                assert {obj.name for obj in remaining} == {"beta", "delta"}
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_form_actions_explicitly_submit() -> None:
    async def run() -> None:
        app, engine = await make_app(OrderedBuilder)
        try:
            transport = httpx.ASGITransport(app=app.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/admin/widgets/new")
            assert response.status_code == 200
            for name in ("_save", "_addanother", "_continue"):
                assert re.search(
                    rf'<button class="btn [^"]+"\s+type="submit"\s+name="{name}">',
                    response.text,
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_list_exposes_row_height_controls() -> None:
    html, _ = asyncio.run(get_list(OrderedBuilder))
    assert 'data-row-height="compact"' in html
    assert 'data-wrap-headers="false"' in html
    for value in ("compact", "medium", "tall", "extra-tall"):
        assert f'data-row-height-choice="{value}"' in html
    for label in ("Short", "Medium", "Tall", "Extra Tall", "Wrap headers"):
        assert label in html
    assert re.search(
        r'<div class="cell-value"><a href="[^"]*/admin/widgets/1">live</a></div>', html
    )


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
            transport = httpx.ASGITransport(app=app.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/api/widgets", params={"filter": json.dumps(tree)})
                page = await client.get("/admin/widgets", params={"filter": json.dumps(tree)})
                html = page.text
            assert {row["name"] for row in response.json()} == {"alpha", "beta"}
            assert "alpha" in html
            assert "beta" in html
            assert "gamma" not in html
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
            transport = httpx.ASGITransport(app=app.app)
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
            transport = httpx.ASGITransport(app=app.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                first = await client.get("/admin/widgets", params={"group": "status,name"})
                second = await client.get("/admin/widgets", params={"group": "status", "offset": 2})
                invalid = await client.get("/admin/widgets", params={"group": "missing"})
            html = first.text
            assert 'data-group-header="1"' in html
            assert 'data-group-header="2"' in html
            assert ">Status<" in html
            assert ">draft<" in html
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
            transport = httpx.ASGITransport(app=app.app)
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
