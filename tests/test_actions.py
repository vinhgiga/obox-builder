from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from obox_builder import Context, DomainEvent, ModelBuilder, Obox, Principal, View, builder
from obox_builder.auth import RBAC, StaffOnly
from obox_builder.conf import Settings
from obox_builder.errors import BuilderValidationError, ImproperlyConfigured, PermissionDenied
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


class Checksheet(Base):
    __tablename__ = "action_checksheets"

    id: Mapped[int] = mapped_column(primary_key=True)
    result: Mapped[str] = mapped_column(sa.String(2), default="")
    status: Mapped[str] = mapped_column(sa.String(10), default="planned")
    owner_id: Mapped[int] = mapped_column(default=1)


class ChecksheetPolicy(RBAC[Checksheet]):
    async def allow(self, action: str, ctx: Context, *, obj: Checksheet | None = None) -> bool:
        if obj is not None and (
            (action == "update" and obj.status == "approved")
            or (action == "approve" and obj.status != "performed")
        ):
            return False
        return await super().allow(action, ctx, obj=obj)

    def scope(self, action: str, ctx: Context) -> sa.ColumnElement[bool]:
        return sa.and_(super().scope(action, ctx), Checksheet.owner_id == ctx.principal.id)


class ChecksheetBuilder(ModelBuilder[Checksheet]):
    fields = ("id", "result", "status", "owner_id")
    read_only_fields = ("status", "owner_id")
    filter_fields = ("status",)
    permissions = ("approve", "export")
    actions = ("approve",)
    policy = ChecksheetPolicy("quality.checksheets")

    class View(View):
        operations = ()

    async def before_save(
        self, instance: Checksheet, changes: Mapping[str, Any], ctx: Context, *, created: bool
    ) -> None:
        if not created and instance.status == "planned" and instance.result in {"OK", "NG"}:
            instance.status = "performed"

    @builder.action(description="Approve selected", permission="approve")
    async def approve(self, objects: Sequence[Checksheet], ctx: Context) -> None:
        for obj in objects:
            obj.status = "approved"


def actor(*permissions: str) -> Principal:
    return Principal(
        id=1,
        is_authenticated=True,
        permissions=frozenset(f"quality.checksheets.{name}" for name in permissions),
    )


READER = actor("list", "retrieve")
EDITOR = actor("list", "retrieve", "create", "update")
APPROVER = actor("list", "retrieve", "approve")


@asynccontextmanager
async def application(
    principal: Principal = APPROVER,
    configured: type[ModelBuilder[Checksheet]] = ChecksheetBuilder,
) -> AsyncIterator[Obox]:
    url = os.getenv("OBOX_TEST_DATABASE_URL", "sqlite+aiosqlite://")
    engine = create_async_engine(
        url, **({"poolclass": StaticPool} if url.startswith("sqlite") else {})
    )
    namespace = f"test_actions_{uuid.uuid4().hex}" if engine.dialect.name == "postgresql" else None
    if namespace:
        async with engine.begin() as connection:
            await connection.execute(sa.schema.CreateSchema(namespace))
        engine = engine.execution_options(schema_translate_map={None: namespace})
    try:
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
        app.register(configured)
        yield app
    finally:
        if namespace:
            async with engine.begin() as connection:
                await connection.execute(sa.schema.DropSchema(namespace, cascade=True))
        await engine.dispose()


async def seed(app: Obox, *statuses: str) -> list[int]:
    async with app.uow(principal=EDITOR) as ctx:
        objects = [Checksheet(status=status, result="OK") for status in statuses]
        ctx.session.add_all(objects)
        await ctx.session.flush()
        return [obj.id for obj in objects]


async def statuses(app: Obox) -> list[str]:
    async with app.uow(principal=READER) as ctx:
        return [obj.status for obj in await app.service(Checksheet).list(ctx=ctx)]


def test_checksheet_lifecycle_and_approved_record_lock() -> None:
    async def scenario() -> None:
        async with application(EDITOR) as app:
            service = app.service(Checksheet)
            async with app.uow(principal=EDITOR) as ctx:
                obj = await service.create({}, ctx=ctx)
                key = obj.id
                assert obj.status == "planned"
            async with app.uow(principal=EDITOR) as ctx:
                obj = await service.update(key, {"result": "OK"}, ctx=ctx)
                assert obj.status == "performed"
            with pytest.raises(PermissionDenied):
                async with app.uow(principal=EDITOR) as ctx:
                    await service.run_action("approve", [key], ctx=ctx)
            async with app.uow(principal=APPROVER) as ctx:
                assert await service.run_action("approve", [key], ctx=ctx) == 1
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app.app), base_url="http://test"
            ) as client:
                page = await client.get(f"/admin/checksheets/{key}")
                assert page.status_code == 200
                assert "approved" in page.text
                assert 'name="_save"' not in page.text
                assert (
                    await client.patch(f"/api/checksheets/{key}", json={"result": "NG"})
                ).status_code == 403
                assert (
                    await client.post(f"/admin/checksheets/{key}", data={"result": "NG"})
                ).status_code == 403
            assert await statuses(app) == ["approved"]

    asyncio.run(scenario())


def test_sync_action_uses_update_permission_and_emits_its_own_event() -> None:
    events: list[DomainEvent] = []

    class ResultBuilder(ChecksheetBuilder):
        actions = ("clear_result",)

        @builder.action(description="Clear result")
        def clear_result(self, objects: Sequence[Checksheet], ctx: Context) -> None:
            for obj in objects:
                obj.result = ""

        async def after_commit(self, event: DomainEvent, ctx: Context) -> None:
            events.append(event)

    async def scenario() -> None:
        async with application(configured=ResultBuilder) as app:
            ids = await seed(app, "performed")
            async with app.uow(principal=EDITOR) as ctx:
                assert await app.service(Checksheet).run_action("clear_result", ids, ctx=ctx) == 1
                assert not events
            assert [(event.action, event.id) for event in events] == [("clear_result", ids[0])]
            async with app.uow(principal=READER) as ctx:
                assert (await app.service(Checksheet).get(ids[0], ctx=ctx)).result == ""

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("principal", "code"), [(APPROVER, 200), (READER, 403), (Principal(), 401)]
)
def test_api_actions_require_the_custom_permission(principal: Principal, code: int) -> None:
    async def scenario() -> None:
        async with application(principal) as app:
            ids = await seed(app, "performed", "performed")
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app.app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/checksheets/actions/approve", json={"record_ids": ids}
                )
                assert response.status_code == code
                if code == 200:
                    assert response.json() == {"count": 2}
            assert await statuses(app) == (["approved"] * 2 if code == 200 else ["performed"] * 2)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("name", "ids", "code"),
    [
        ("missing", [1], 422),
        ("approve", [], 422),
        ("approve", [1, "01"], 422),
        ("approve", [1, 999], 404),
        ("approve", [1, 2], 403),
        ("approve", ["bad"], 422),
    ],
)
def test_invalid_action_batches_leave_all_records_unchanged(
    name: str, ids: list[Any], code: int
) -> None:
    async def scenario() -> None:
        async with application() as app:
            await seed(app, "performed", "approved")
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app.app), base_url="http://test"
            ) as client:
                response = await client.post(
                    f"/api/checksheets/actions/{name}", json={"record_ids": ids}
                )
                assert response.status_code == code
            assert await statuses(app) == ["performed", "approved"]

    asyncio.run(scenario())


def test_actions_apply_row_scope_and_api_csrf() -> None:
    async def scenario() -> None:
        async with application() as app:
            ids = await seed(app, "performed")
            async with app.uow(principal=EDITOR) as ctx:
                ctx.session.add(Checksheet(id=2, owner_id=2, status="performed"))
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app.app), base_url="http://test"
            ) as client:
                hidden = await client.post(
                    "/api/checksheets/actions/approve", json={"record_ids": [*ids, 2]}
                )
                assert hidden.status_code == 404
                client.cookies.set("session", "cookie")
                csrf = await client.post(
                    "/api/checksheets/actions/approve", json={"record_ids": ids}
                )
                assert csrf.status_code == 403
            assert await statuses(app) == ["performed"]

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["handler", "check", "commit"])
def test_action_failure_rolls_back_and_poisons_the_unit_of_work(failure: str) -> None:
    class FailingPolicy(ChecksheetPolicy):
        async def check(self, action: str, instance: Checksheet, ctx: Context) -> bool:
            return failure != "check" and await super().check(action, instance, ctx)

    class FailingBuilder(ChecksheetBuilder):
        policy = FailingPolicy("quality.checksheets")

        @builder.action(description="Approve", permission="approve")
        async def approve(self, objects: Sequence[Checksheet], ctx: Context) -> None:
            await super().approve(objects, ctx)
            if failure == "handler":
                await ctx.session.flush()
                raise BuilderValidationError({"__all__": ["Failed"]})
            if failure == "commit":
                await ctx.session.commit()

    async def scenario() -> None:
        async with application(configured=FailingBuilder) as app:
            ids = await seed(app, "performed", "performed")

            async def caught_failure() -> None:
                async with app.uow(principal=APPROVER) as ctx:
                    with pytest.raises(
                        (BuilderValidationError, PermissionDenied, ImproperlyConfigured)
                    ):
                        await app.service(Checksheet).run_action("approve", ids, ctx=ctx)

            with pytest.raises(ImproperlyConfigured):
                await caught_failure()
            assert await statuses(app) == ["performed", "performed"]

    asyncio.run(scenario())


@pytest.mark.parametrize("across", ["0", "1"])
def test_admin_actions_work_without_delete_permission_and_respect_filters(across: str) -> None:
    async def scenario() -> None:
        async with application() as app:
            ids = await seed(app, "performed", "performed", "planned")
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app.app), base_url="http://test"
            ) as client:
                page = await client.get("/admin/checksheets")
                assert 'value="approve"' in page.text
                assert 'value="delete"' not in page.text
                assert 'name="record_ids"' in page.text
                response = await client.post(
                    "/admin/checksheets/bulk-actions?filter.status=performed&limit=1",
                    data={
                        "csrf_token": "",
                        "action": "approve",
                        "select_across": across,
                        "record_ids": [str(ids[0])],
                    },
                )
                assert response.status_code == 303
            assert await statuses(app) == [
                "approved",
                "approved" if across == "1" else "performed",
                "planned",
            ]

    asyncio.run(scenario())


def test_admin_hides_and_rejects_actions_without_permission() -> None:
    async def scenario() -> None:
        async with application(READER) as app:
            ids = await seed(app, "performed")
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app.app), base_url="http://test"
            ) as client:
                page = await client.get("/admin/checksheets")
                assert 'value="approve"' not in page.text
                response = await client.post(
                    "/admin/checksheets/bulk-actions",
                    data={
                        "csrf_token": "",
                        "action": "approve",
                        "select_across": "0",
                        "record_ids": [str(ids[0])],
                    },
                )
                assert response.status_code == 422
            assert await statuses(app) == ["performed"]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "attributes",
    [
        {"permissions": ("approve", "approve")},
        {"permissions": ("bad.name",)},
        {"permissions": ("update",)},
        {"actions": ("missing",)},
        {"actions": ("before_save",)},
        {"actions": ("approve", "approve")},
        {"actions": "approve"},
        {"permissions": ()},
    ],
)
def test_invalid_action_configuration_fails_at_registration(attributes: dict[str, Any]) -> None:
    async def scenario() -> None:
        configured = type("InvalidBuilder", (ChecksheetBuilder,), attributes)
        with pytest.raises(ImproperlyConfigured):
            async with application(configured=configured):
                pass

    asyncio.run(scenario())


@pytest.mark.skipif(
    not os.getenv("OBOX_TEST_DATABASE_URL", "").startswith("postgresql"),
    reason="Requires an isolated PostgreSQL schema",
)
def test_action_locks_records_until_the_approval_commits() -> None:
    async def scenario() -> None:
        async with application() as app:
            ids = await seed(app, "performed")
            service = app.service(Checksheet)

            async def edit() -> None:
                async with app.uow(principal=EDITOR) as ctx:
                    await ctx.session.execute(sa.text("SET LOCAL lock_timeout = '100ms'"))
                    await service.update(ids[0], {"result": "NG"}, ctx=ctx)

            async with app.uow(principal=APPROVER) as ctx:
                await service.run_action("approve", ids, ctx=ctx)
                with pytest.raises(DBAPIError) as error:
                    await edit()
                assert getattr(error.value.orig, "sqlstate", None) == "55P03"
            with pytest.raises(PermissionDenied):
                await edit()
            assert await statuses(app) == ["approved"]

    asyncio.run(scenario())
