from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx
import sqlalchemy as sa
from fastapi import FastAPI
from obox_builder import ModelBuilder, Obox, Principal, View, builder
from obox_builder.auth import PublicReadOnly, StaffOnly
from obox_builder.conf import Settings
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


class Parent(Base):
    __tablename__ = "record_page_parents"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(50))
    status: Mapped[str] = mapped_column(sa.String(20), default="draft")
    children: Mapped[list[Child]] = relationship(
        back_populates="parent",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="raise",
    )


class Child(Base):
    __tablename__ = "record_page_children"

    id: Mapped[int] = mapped_column(primary_key=True)
    parent_id: Mapped[int] = mapped_column(
        sa.ForeignKey("record_page_parents.id", ondelete="CASCADE")
    )
    label: Mapped[str] = mapped_column(sa.String(50))
    score: Mapped[int] = mapped_column(default=0)
    parent: Mapped[Parent] = relationship(back_populates="children", lazy="raise")


class ChildInline(builder.Inline[Child]):
    model = Child


class ChildView(View):
    list_display = ("parent", "label", "score")


class ChildBuilder(ModelBuilder[Child]):
    fields = ("parent_id", "label", "score")
    label_field = "label"
    slug = "children"
    View = ChildView


class ParentView(View):
    list_display = ("name", "status")
    related = ("children",)


class ParentBuilder(ModelBuilder[Parent]):
    fields = ("name", "status")
    label_field = "name"
    slug = "parents"
    inlines: tuple[type[builder.Inline[Any]], ...] = (ChildInline,)
    View = ParentView


class RelatedParentBuilder(ParentBuilder):
    inlines = ()


class ReadOnlyPolicy(StaffOnly[Any]):
    async def allow(self, action: str, ctx: Any, *, obj: Any = None) -> bool:
        return action in {"list", "retrieve"} and await super().allow(action, ctx, obj=obj)


class ReadOnlyParentBuilder(ParentBuilder):
    policy = ReadOnlyPolicy()


class PublicParentBuilder(ParentBuilder):
    policy = PublicReadOnly[Parent]()


class HiddenChildPolicy(StaffOnly[Child]):
    async def allow(self, action: str, ctx: Any, *, obj: Any = None) -> bool:
        if action == "retrieve" and obj is not None and obj.label == "secret":
            return False
        return await super().allow(action, ctx, obj=obj)


class HiddenChildBuilder(ChildBuilder):
    policy = HiddenChildPolicy()


class HiddenParentPolicy(StaffOnly[Parent]):
    async def allow(self, action: str, ctx: Any, *, obj: Any = None) -> bool:
        if action == "retrieve" and obj is not None and obj.name == "secret":
            return False
        return await super().allow(action, ctx, obj=obj)


class HiddenParentBuilder(ParentBuilder):
    policy = HiddenParentPolicy()


class UpdateOnlyPolicy(StaffOnly[Parent]):
    async def allow(self, action: str, ctx: Any, *, obj: Any = None) -> bool:
        return action in {"list", "update"} and await super().allow(action, ctx, obj=obj)


class UpdateOnlyParentBuilder(ParentBuilder):
    policy = UpdateOnlyPolicy()


class DisabledView(ParentView):
    enabled = False


class DisabledParentBuilder(ParentBuilder):
    View = DisabledView


STAFF = Principal(id=1, is_authenticated=True, is_staff=True)


async def make_app(
    parent_builder: type[ModelBuilder[Parent]] = ParentBuilder,
    child_builder: type[ModelBuilder[Child]] = ChildBuilder,
    *,
    principal: Principal | None = STAFF,
) -> tuple[Obox, Any]:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    app = Obox(
        FastAPI(),
        settings=Settings(
            session_factory=async_sessionmaker(engine, expire_on_commit=False),
            principal_dependency=lambda: principal,
            default_policy=StaffOnly(),
            csrf_secret="x" * 32,
        ),
    )
    app.register(parent_builder)
    app.register(child_builder)
    return app, engine


async def seed(engine: Any, parent: str = "alpha") -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory.begin() as session:
        session.add(
            Parent(
                name=parent,
                status="live",
                children=[
                    Child(label="first", score=1),
                    Child(label="second", score=2),
                ],
            )
        )


async def get(app: Obox, path: str) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app.app), base_url="http://test"
    ) as client:
        return await client.get(path)


async def post(app: Obox, path: str, data: dict[str, str]) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app.app), base_url="http://test"
    ) as client:
        return await client.post(path, data=data)


def test_list_links_first_displayed_column() -> None:
    async def scenario() -> None:
        app, engine = await make_app()
        try:
            await seed(engine)
            parents = await get(app, "/admin/parents")
            children = await get(app, "/admin/children")
        finally:
            await engine.dispose()

        assert re.search(r'<a href="[^"]*/admin/parents/1">alpha</a>', parents.text)
        assert ">Record</th>" not in parents.text
        assert re.search(r'<a href="[^"]*/admin/children/1">alpha</a>', children.text)
        assert '<div class="cell-value">first</div>' in children.text
        assert ">Record</th>" not in children.text

    asyncio.run(scenario())


def test_editable_detail_renders_form_and_saves() -> None:
    async def scenario() -> None:
        app, engine = await make_app()
        try:
            await seed(engine)
            detail = await get(app, "/admin/parents/1")
            edit = await get(app, "/admin/parents/1/edit")
            continued = await post(
                app,
                "/admin/parents/1",
                {"name": "updated", "status": "paused", "_continue": ""},
            )
            another = await post(
                app,
                "/admin/parents/1",
                {"name": "updated", "status": "paused", "_addanother": ""},
            )
            saved = await post(
                app,
                "/admin/parents/1",
                {"name": "updated", "status": "paused", "_save": ""},
            )
            async with async_sessionmaker(engine)() as session:
                parent = await session.get(Parent, 1)
                assert parent is not None
                assert (parent.name, parent.status) == ("updated", "paused")
        finally:
            await engine.dispose()

        assert detail.status_code == 200
        for name in ("_save", "_addanother", "_continue"):
            assert f'name="{name}"' in detail.text
        assert "data-inline" in detail.text
        assert "/admin/parents/1/delete" in detail.text
        assert edit.status_code == 303
        assert edit.headers["location"].endswith("/admin/parents/1")
        assert continued.status_code == 303
        assert continued.headers["location"].endswith("/admin/parents/1")
        assert another.headers["location"].endswith("/admin/parents/new")
        assert saved.headers["location"].endswith("/admin/parents")

    asyncio.run(scenario())


def test_update_only_user_can_open_editable_detail() -> None:
    async def scenario() -> None:
        app, engine = await make_app(UpdateOnlyParentBuilder)
        try:
            await seed(engine)
            detail = await get(app, "/admin/parents/1")
            listing = await get(app, "/admin/parents")
        finally:
            await engine.dispose()

        assert detail.status_code == 200
        assert 'name="_save"' in detail.text
        assert ">alpha</a>" not in listing.text

    asyncio.run(scenario())


def test_public_read_only_detail_for_anonymous_user() -> None:
    async def scenario() -> None:
        app, engine = await make_app(PublicParentBuilder, principal=None)
        try:
            await seed(engine)
            detail = await get(app, "/admin/parents/1")
            listing = await get(app, "/admin/parents")
        finally:
            await engine.dispose()

        assert detail.status_code == 200
        assert '<dl class="row">' in detail.text
        assert 'name="_save"' not in detail.text
        assert "data-inline" not in detail.text
        assert re.search(r'<a href="[^"]*/admin/parents/1">alpha</a>', listing.text)

    asyncio.run(scenario())


def test_read_only_detail_has_no_edit_form() -> None:
    async def scenario() -> None:
        app, engine = await make_app(ReadOnlyParentBuilder)
        try:
            await seed(engine)
            detail = await get(app, "/admin/parents/1")
            edit = await get(app, "/admin/parents/1/edit")
            delete = await get(app, "/admin/parents/1/delete")
        finally:
            await engine.dispose()

        assert detail.status_code == 200
        assert '<dl class="row">' in detail.text
        assert 'name="_save"' not in detail.text
        assert "data-inline" not in detail.text
        assert "<h2>Children</h2>" in detail.text
        assert re.search(r'<a href="[^"]*/admin/children/1">alpha</a>', detail.text)
        assert edit.status_code == 403
        assert delete.status_code == 403

    asyncio.run(scenario())


def test_editable_detail_keeps_related_without_inline() -> None:
    async def scenario() -> None:
        app, engine = await make_app(RelatedParentBuilder)
        try:
            await seed(engine)
            detail = await get(app, "/admin/parents/1")
        finally:
            await engine.dispose()

        assert 'name="_save"' in detail.text
        assert "<h2>Children</h2>" in detail.text
        assert re.search(r'<a href="[^"]*/admin/children/1">alpha</a>', detail.text)

    asyncio.run(scenario())


def test_related_rows_link_only_when_retrieve_is_permitted() -> None:
    async def scenario() -> None:
        app, engine = await make_app(ReadOnlyParentBuilder, HiddenChildBuilder)
        try:
            async with async_sessionmaker(engine).begin() as session:
                session.add(
                    Parent(
                        name="alpha",
                        children=[
                            Child(label="visible"),
                            Child(label="secret"),
                        ],
                    )
                )
            detail = await get(app, "/admin/parents/1")
        finally:
            await engine.dispose()

        assert re.search(r'<a href="[^"]*/admin/children/1">alpha</a>', detail.text)
        assert not re.search(r'<a href="[^"]*/admin/children/2"', detail.text)
        assert "secret" in detail.text

    asyncio.run(scenario())


def test_view_disabled_keeps_edit_form_working() -> None:
    async def scenario() -> None:
        app, engine = await make_app(DisabledParentBuilder)
        try:
            await seed(engine)
            edit = await get(app, "/admin/parents/1/edit")
            continued = await post(
                app,
                "/admin/parents/1/edit",
                {"name": "updated", "status": "paused", "_continue": ""},
            )
            detail = await get(app, "/admin/parents/1")
        finally:
            await engine.dispose()

        assert edit.status_code == 200
        assert 'name="_save"' in edit.text
        assert "/admin/parents/1/delete" not in edit.text
        assert continued.status_code == 303
        assert continued.headers["location"].endswith("/admin/parents/1/edit")
        assert detail.status_code == 404

    asyncio.run(scenario())


def test_inline_extra_adds_forms_after_existing_records() -> None:
    async def scenario() -> None:
        app, engine = await make_app()
        try:
            await seed(engine)
            detail = await get(app, "/admin/parents/1")
        finally:
            await engine.dispose()

        assert detail.status_code == 200
        assert re.search(r'name="inline:0:_total"\s+value="5"', detail.text)
        assert len(re.findall(r'name="inline:0:\d+:_pk"', detail.text)) == 5
        assert len(re.findall(r'name="inline:0:\d+:_pk"\s+value=""', detail.text)) == 3

    asyncio.run(scenario())


def test_delete_link_opens_confirmation_page() -> None:
    async def scenario() -> None:
        app, engine = await make_app()
        try:
            await seed(engine)
            detail = await get(app, "/admin/parents/1")
            confirm = await get(app, "/admin/parents/1/delete")
            deleted = await post(app, "/admin/parents/1/delete", {"csrf_token": "confirmation"})
            async with async_sessionmaker(engine)() as session:
                assert await session.get(Parent, 1) is None
        finally:
            await engine.dispose()

        assert 'class="btn btn-danger"' in detail.text
        assert "Confirm deletion" not in detail.text
        assert confirm.status_code == 200
        assert "Are you sure?" in confirm.text
        assert "alpha" in confirm.text
        assert 'name="_save"' not in confirm.text
        assert deleted.status_code == 303
        assert deleted.headers["location"].endswith("/admin/parents")

    asyncio.run(scenario())


def test_list_row_without_retrieve_has_no_link() -> None:
    async def scenario() -> None:
        app, engine = await make_app(HiddenParentBuilder)
        try:
            async with async_sessionmaker(engine).begin() as session:
                session.add_all(
                    [
                        Parent(name="visible"),
                        Parent(name="secret"),
                    ]
                )
            listing = await get(app, "/admin/parents")
        finally:
            await engine.dispose()

        assert re.search(r'<a href="[^"]*/admin/parents/1">visible</a>', listing.text)
        assert '<div class="cell-value">secret</div>' in listing.text
        assert "secret</a>" not in listing.text

    asyncio.run(scenario())
