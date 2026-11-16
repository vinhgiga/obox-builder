import asyncio
import re
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from obox._auth import RBAC, Principal
from obox._config import RuntimeConfig
from obox._context import Context
from obox._runtime import _Runtime
from obox.contrib import resources as builder
from obox.contrib.resources import Resource as ResourceDefinition
from obox.contrib.resources.views import ViewOptions
from obox.core.exceptions import ImproperlyConfigured
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.pool import StaticPool

from tests.helpers import with_builders


class Base(DeclarativeBase):
    pass


class Parent(Base):
    __tablename__ = "relation_display_parents"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(50))
    private_value: Mapped[str] = mapped_column(sa.String(50))

    def __str__(self) -> str:
        return self.private_value


class Child(Base):
    __tablename__ = "relation_display_children"
    id: Mapped[int] = mapped_column(primary_key=True)
    parent_id: Mapped[int] = mapped_column(sa.ForeignKey("relation_display_parents.id"))
    name: Mapped[str] = mapped_column(sa.String(50))
    parent: Mapped[Parent] = relationship(lazy="raise")


class ParentBuilder(ResourceDefinition[Parent]):
    model = Parent
    fields = ("name",)
    label_field: str | None = "name"
    slug = "parents"


class ChildListView(ViewOptions):
    list_display = ("parent", "name")


class ChildBuilder(ResourceDefinition[Child]):
    model = Child
    fields: tuple[str, ...] = ("parent_id", "name")
    filter_fields = ("parent_id",)
    ordering = ("parent_id", "id")
    slug = "children"
    view_options: type[builder.ViewOptions] = ChildListView


class ScopedParentPolicy(RBAC[Parent]):
    def scope(self, action: str, ctx: Context) -> Any:
        return Parent.id != 2 if action == "retrieve" else sa.true()


class ObjectDeniedParentPolicy(RBAC[Parent]):
    async def allow(self, action: str, ctx: Context, *, obj: Any = None) -> bool:
        if obj is not None and obj.name == "Hidden":
            return False
        return await super().allow(action, ctx, obj=obj)


class LabelDeniedParentPolicy(RBAC[Parent]):
    def read_fields(self, ctx: Context, *, obj: Any = None) -> frozenset[str] | None:
        return frozenset() if obj is not None else None


class ChildNameOnlyPolicy(RBAC[Child]):
    def read_fields(self, ctx: Context, *, obj: Any = None) -> frozenset[str] | None:
        return frozenset({"name"}) if obj is not None else None


class ScopedParentBuilder(ParentBuilder):
    policy = ScopedParentPolicy()


class ObjectDeniedParentBuilder(ParentBuilder):
    policy = ObjectDeniedParentPolicy()


class LabelDeniedParentBuilder(ParentBuilder):
    policy = LabelDeniedParentPolicy()


class UnlabeledParentBuilder(ParentBuilder):
    label_field = None


class ChildNameOnlyBuilder(ChildBuilder):
    policy = ChildNameOnlyPolicy()


class DynamicChildView(ViewOptions):
    list_display = ("name",)

    def get_list_display(self, ctx: Context) -> tuple[str, ...]:
        return ("parent", "name")


class DynamicChildBuilder(ChildBuilder):
    view_options = DynamicChildView


class MissingForeignKeyBuilder(ChildBuilder):
    fields = ("name",)


STAFF = Principal(id=1, is_authenticated=True, is_staff=True, is_superuser=True)


async def make_app(
    *registrations: tuple[type[Any], type[ResourceDefinition[Any]]],
) -> tuple[_Runtime, Any]:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    app = _Runtime(
        FastAPI(),
        settings=with_builders(
            RuntimeConfig(
                session_factory=async_sessionmaker(engine, expire_on_commit=False),
                principal_dependency=lambda: STAFF,
                default_policy=RBAC(),
                csrf_secret="x" * 32,
            ),
            *(definition for _, definition in registrations),
        ),
    )
    return (app, engine)


async def seed(app: _Runtime) -> None:
    async with app.uow(principal=STAFF) as ctx:
        visible = Parent(name="Visible", private_value="TOP-SECRET")
        hidden = Parent(name="Hidden", private_value="TOP-SECRET-HIDDEN")
        ctx.session.add_all([visible, hidden])
        await ctx.session.flush()
        ctx.session.add_all(
            [Child(parent_id=visible.id, name="alpha"), Child(parent_id=hidden.id, name="beta")]
        )


async def get_html(app: _Runtime) -> str:
    transport = httpx.ASGITransport(app=app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/admin/admin/children")
        assert response.status_code == 200
        return response.text


def test_list_displays_relation_label_and_keeps_api_raw() -> None:

    async def run() -> None:
        app, engine = await make_app((Parent, ParentBuilder), (Child, ChildBuilder))
        try:
            await seed(app)
            html = await get_html(app)
            assert 'data-list-column="parent">Parent</th>' in html
            assert "Visible" in html
            assert "Hidden" in html
            assert "TOP-SECRET" not in html
            assert '"name": "parent_id"' in html
            transport = httpx.ASGITransport(app=app.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/api/admin/children")
                form = (await client.get("/admin/admin/children/new")).text
            assert response.status_code == 200
            assert response.json()["items"] == [
                {"id": "1", "parentId": "1", "name": "alpha"},
                {"id": "2", "parentId": "2", "name": "beta"},
            ]
            assert "Visible" in form
            assert "TOP-SECRET" not in form
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_relation_label_respects_target_scope() -> None:

    async def run() -> None:
        app, engine = await make_app((Parent, ScopedParentBuilder), (Child, ChildBuilder))
        try:
            await seed(app)
            html = await get_html(app)
            assert "Visible" in html
            assert "Hidden" not in html
            assert 'title="2"' in html
            assert re.search('<a href="[^"]*/admin/admin/children/2">2</a>', html)
            assert "beta" in html
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_relation_label_respects_target_object_permission() -> None:

    async def run() -> None:
        app, engine = await make_app((Parent, ObjectDeniedParentBuilder), (Child, ChildBuilder))
        try:
            await seed(app)
            html = await get_html(app)
            assert "Visible" in html
            assert "Hidden" not in html
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_relation_label_requires_target_field_readability() -> None:

    async def run() -> None:
        app, engine = await make_app((Parent, LabelDeniedParentBuilder), (Child, ChildBuilder))
        try:
            await seed(app)
            html = await get_html(app)
            assert "Visible" not in html
            assert "Hidden" not in html
            assert "alpha" in html
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_relation_without_label_field_falls_back_to_raw_key() -> None:

    async def run() -> None:
        app, engine = await make_app((Parent, UnlabeledParentBuilder), (Child, ChildBuilder))
        try:
            await seed(app)
            html = await get_html(app)
            assert "Visible" not in html
            assert "TOP-SECRET" not in html
            assert 'title="1"' in html
            assert re.search('<a href="[^"]*/admin/admin/children/1">1</a>', html)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_relation_is_hidden_when_parent_foreign_key_is_not_readable() -> None:

    async def run() -> None:
        app, engine = await make_app((Parent, ParentBuilder), (Child, ChildNameOnlyBuilder))
        try:
            await seed(app)
            html = await get_html(app)
            assert "Visible" not in html
            assert "Hidden" not in html
            assert "alpha" in html
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_relation_display_requires_registered_target() -> None:

    async def run() -> None:
        app, engine = await make_app((Child, ChildBuilder))
        try:
            with pytest.raises(ImproperlyConfigured, match="relation's target"):
                app.check(strict=True)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_dynamic_list_display_cannot_add_relation() -> None:

    async def run() -> None:
        app, engine = await make_app((Parent, ParentBuilder), (Child, DynamicChildBuilder))
        try:
            await seed(app)
            html = await get_html(app)
            assert '<th scope="col">Parent</th>' not in html
            assert "Visible" not in html
            assert "alpha" in html
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_relation_display_requires_foreign_key_in_fields() -> None:

    async def run() -> None:
        with pytest.raises(ImproperlyConfigured, match="requires its foreign key"):
            await make_app((Parent, ParentBuilder), (Child, MissingForeignKeyBuilder))

    asyncio.run(run())
