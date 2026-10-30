import asyncio
from collections.abc import Mapping
from typing import Any

import pytest
import sqlalchemy as sa
from obox_builder import Form, ModelBuilder, Obox, Principal
from obox_builder.auth import StaffOnly
from obox_builder.builder.ui import form_fields
from obox_builder.context import Context
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


class Widget(Base):
    __tablename__ = "widgets"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(50))
    status: Mapped[str] = mapped_column(sa.String(20), default="draft")


class RecordingBuilder(ModelBuilder[Widget]):
    def __init__(self) -> None:
        self.saved: list[tuple[bool, dict[str, Any], str]] = []
        self.deleted: list[int] = []
        self.committed: list[tuple[str, int]] = []

    async def before_save(
        self,
        instance: Widget,
        changes: Mapping[str, Any],
        ctx: Context,
        *,
        created: bool,
    ) -> None:
        self.saved.append((created, dict(changes), instance.name))
        instance.name = instance.name.strip()

    async def before_delete(self, instance: Widget, ctx: Context) -> None:
        self.deleted.append(instance.id)

    async def after_commit(self, event: Any, ctx: Context) -> None:
        self.committed.append((event.action, event.id))


class FailingBuilder(ModelBuilder[Widget]):
    async def before_save(
        self,
        instance: Widget,
        changes: Mapping[str, Any],
        ctx: Context,
        *,
        created: bool,
    ) -> None:
        raise RuntimeError("before_save failed")


class OrderedForm(Form):
    fields = ("status", "name")


class OrderedBuilder(ModelBuilder[Widget]):
    Form = OrderedForm


class ReorderedForm(OrderedForm):
    def get_fields(self, ctx: Context, *, action: str, instance: Any = None) -> tuple[str, ...]:
        return tuple(reversed(super().get_fields(ctx, action=action, instance=instance)))


class ReorderedBuilder(ModelBuilder[Widget]):
    Form = ReorderedForm


class DuplicateForm(OrderedForm):
    def get_fields(self, ctx: Context, *, action: str, instance: Any = None) -> tuple[str, ...]:
        return ("name", "name", "status")


class DuplicateBuilder(ModelBuilder[Widget]):
    Form = DuplicateForm


async def make_app(builder: type[ModelBuilder[Widget]]) -> tuple[Obox, Any]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    app = Obox(
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        principal_dependency=lambda: None,
        default_policy=StaffOnly(),
        csrf_secret="x" * 32,
    )
    app.register(Widget, builder=builder)
    return app, engine


def test_before_save_receives_created_and_changes() -> None:
    async def scenario() -> None:
        app, engine = await make_app(RecordingBuilder)
        builder = app.get_builder(Widget)
        principal = Principal(id=1, is_authenticated=True, is_staff=True)
        try:
            async with app.uow(principal=principal) as ctx:
                created = await app.service(Widget).create({"name": "  alpha  "}, ctx=ctx)
                widget_id = created.id
                assert created.name == "alpha"
            async with app.uow(principal=principal) as ctx:
                updated = await app.service(Widget).update(widget_id, {"name": "  beta  "}, ctx=ctx)
                assert updated.name == "beta"
            async with app.uow(principal=principal) as ctx:
                await app.service(Widget).delete(widget_id, ctx=ctx)

            assert builder.saved == [
                (True, {"name": "  alpha  "}, "  alpha  "),
                (False, {"name": "  beta  "}, "  beta  "),
            ]
            assert builder.deleted == [widget_id]
            assert builder.committed == [
                ("create", widget_id),
                ("update", widget_id),
                ("delete", widget_id),
            ]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_before_save_failure_rolls_back() -> None:
    async def scenario() -> None:
        app, engine = await make_app(FailingBuilder)
        principal = Principal(id=1, is_authenticated=True, is_staff=True)
        try:
            with pytest.raises(RuntimeError, match="before_save failed"):
                async with app.uow(principal=principal) as ctx:
                    await app.service(Widget).create({"name": "alpha"}, ctx=ctx)
            async with app.uow(principal=principal) as ctx:
                assert await app.service(Widget).list(ctx=ctx) == []
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_form_fields_order_comes_from_form_fields() -> None:
    async def scenario() -> None:
        app, engine = await make_app(OrderedBuilder)
        builder = app.get_builder(Widget)
        principal = Principal(id=1, is_authenticated=True, is_staff=True)
        try:
            assert builder.form_fields == ("status", "name")
            async with app.uow(principal=principal, source="ui") as ctx:
                controls = await form_fields(builder, app.service(Widget), ctx, None)
            assert [control["name"] for control in controls] == ["status", "name"]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_form_get_fields_order_overrides_static_order() -> None:
    async def scenario() -> None:
        app, engine = await make_app(ReorderedBuilder)
        builder = app.get_builder(Widget)
        principal = Principal(id=1, is_authenticated=True, is_staff=True)
        try:
            async with app.uow(principal=principal, source="ui") as ctx:
                controls = await form_fields(builder, app.service(Widget), ctx, None)
            assert [control["name"] for control in controls] == ["name", "status"]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_form_get_fields_deduplicates() -> None:
    async def scenario() -> None:
        app, engine = await make_app(DuplicateBuilder)
        builder = app.get_builder(Widget)
        principal = Principal(id=1, is_authenticated=True, is_staff=True)
        try:
            async with app.uow(principal=principal, source="ui") as ctx:
                controls = await form_fields(builder, app.service(Widget), ctx, None)
            assert [control["name"] for control in controls] == ["name", "status"]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_form_has_no_layout_api() -> None:
    assert not hasattr(Form, "layout")
    assert not hasattr(Form, "get_layout")
