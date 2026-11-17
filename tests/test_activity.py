import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from obox._auth import Principal
from obox._config import RuntimeConfig
from obox._runtime import _Runtime
from obox.contrib.activity import Activity
from obox.contrib.activity.models import Base as HistoryBase
from obox.contrib.activity.models import Event
from obox.core.exceptions import ImproperlyConfigured
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from tests.storage import database


class Base(DeclarativeBase):
    pass


class Entry(Base):
    __tablename__ = "history_entries"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant: Mapped[int]
    name: Mapped[str]
    private_note: Mapped[str]


ACTOR = Principal(
    id=1, is_authenticated=True, permissions=frozenset({"tests.view_entry", "tests.comment_entry"})
)


class History(Activity):
    def get_queryset(self, model: type[Any], ctx: Any) -> Any:
        return sa.select(model).where(Entry.tenant == ctx.principal.id)

    def get_fields(self, model: type[Any], ctx: Any, obj: Any = None) -> tuple[str, ...]:
        return ("name",)


@asynccontextmanager
async def runtime(path: Path) -> AsyncIterator[Any]:
    async with database(path / "history.sqlite3", (Base.metadata, HistoryBase.metadata)) as factory:
        history = History(models={Entry: ("name", "private_note")})
        app = _Runtime(
            FastAPI(),
            settings=RuntimeConfig(
                session_factory=factory, principal_dependency=lambda: ACTOR, csrf_secret="x" * 32
            ),
            features=(history,),
        )
        async with (
            app.app.router.lifespan_context(app.app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app.app),
                base_url="http://test",
            ) as client,
        ):
            yield app, history, client


def test_history_captures_same_transaction_actor_changes_and_rollback(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with runtime(tmp_path) as (app, history, _):
            async with app.uow(principal=ACTOR) as ctx:
                entry = Entry(name="First", tenant=1, private_note="Never publish")
                ctx.session.add(entry)
                await ctx.session.flush()
                entry.name = "Second"
                await ctx.session.flush()
                entry.name = "Second"
                await ctx.session.flush()
            async with app.uow(principal=ACTOR) as ctx:
                events = list(await ctx.session.scalars(sa.select(Event).order_by(Event.id)))
                assert [event.action for event in events] == ["create", "update"]
                assert events[1].changes == [
                    {"field": "name", "kind": "text", "before": "First", "after": "Second"}
                ]
                assert all(event.actor_id == "1" for event in events)

            async def rollback() -> None:
                async with app.uow(principal=ACTOR) as ctx:
                    entry = await ctx.session.get(Entry, 1)
                    entry.name = "Rolled back"
                    await ctx.session.flush()
                    raise RuntimeError("rollback")

            with pytest.raises(RuntimeError, match="rollback"):
                await rollback()
            async with app.uow(principal=ACTOR) as ctx:
                assert await ctx.session.scalar(sa.select(Entry.name)) == "Second"
                assert await ctx.session.scalar(sa.select(sa.func.count()).select_from(Event)) == 2
            async with (
                app.session_factory() as session,
                session.begin(),
                history.context(session, actor=ACTOR),
            ):
                entry = await session.get(Entry, 1)
                await session.delete(entry)
            async with app.session_factory() as session:
                assert (
                    await session.scalar(sa.select(Event.action).order_by(Event.id.desc()))
                    == "delete"
                )

    asyncio.run(scenario())


def test_history_is_scoped_redacted_paginated_and_signed(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with runtime(tmp_path) as (app, _, client):
            async with app.uow(principal=ACTOR) as ctx:
                entry = Entry(name="First", tenant=1, private_note="Sensitive")
                ctx.session.add_all(
                    [entry, Entry(name="Other tenant", tenant=2, private_note="Other private_note")]
                )
                await ctx.session.flush()
                entry.name = "Second"
                await ctx.session.flush()
            path = "/api/activity/tests.entry/1/activity"
            response = await client.get(path, params={"limit": 1})
            assert response.status_code == 200, response.text
            assert "Sensitive" not in response.text
            assert response.json()["items"][0]["changes"][0]["after"] == "Second"
            cursor = response.json()["page"]["nextCursor"]
            assert cursor
            following = await client.get(path, params={"limit": 1, "cursor": cursor})
            assert following.json()["items"][0]["action"] == "create"
            assert (await client.get(path, params={"cursor": cursor + "x"})).status_code == 422
            assert (await client.get("/api/activity/tests.entry/2/activity")).status_code == 404
            assert (await client.get(path, params={"limit": 1000})).status_code == 422
            resolver = app.settings.principal_dependency
            app.app.dependency_overrides[resolver] = lambda: Principal()
            assert (await client.get(path)).status_code == 401

    asyncio.run(scenario())


def test_history_rejects_bulk_writes_that_bypass_row_hooks(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with runtime(tmp_path) as (app, _, _):
            with pytest.raises(ImproperlyConfigured, match="Bulk writes"):
                async with app.uow(principal=ACTOR) as ctx:
                    await ctx.session.execute(sa.update(Entry).values(name="Unsafe"))

    asyncio.run(scenario())


def test_comments_enforce_author_and_optimistic_concurrency(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with runtime(tmp_path) as (app, _, client):
            async with app.uow(principal=ACTOR) as ctx:
                ctx.session.add(Entry(name="Entry", tenant=1, private_note="Sensitive"))
            path = "/api/activity/tests.entry/1/comments"
            created = await client.post(path, json={"body": "Hello <script>"})
            assert created.status_code == 201, created.text
            location = created.headers["location"]
            etag = created.headers["etag"]
            assert (await client.patch(location, json={"body": "Changed"})).status_code == 428
            updated = await client.patch(
                location, json={"body": "Changed"}, headers={"if-match": etag}
            )
            assert updated.status_code == 200, updated.text
            assert (
                await client.patch(location, json={"body": "Stale"}, headers={"if-match": etag})
            ).status_code == 412
            assert (
                await client.delete(location, headers={"if-match": updated.headers["etag"]})
            ).status_code == 204
            assert (await client.get(location)).status_code == 404

    asyncio.run(scenario())
