import asyncio
import re
from decimal import Decimal

import httpx
import pytest
from obox import ModelResource, Obox
from obox.api import Context
from obox.core.exceptions import ImproperlyConfigured, ResourceConflict
from obox.core.principal import ANONYMOUS
from obox.db.engine import default_session_factory
from obox.db.transaction import atomic
from sqlalchemy import select
from sqlalchemy.exc import InvalidRequestError

from tests.test_forms_integration import Item, configured
from tests.test_resources import authenticated_app


def test_on_commit_runs_in_order_after_outer_commit_and_session_close() -> None:
    calls: list[str] = []

    async def scenario() -> None:
        async with configured():
            runtime = Obox(authenticated_app())._runtime
            async with runtime.uow(principal=ANONYMOUS) as ctx:
                ctx.session.add(Item(name="visible", price=Decimal("2")))

                async def verify() -> None:
                    with pytest.raises(InvalidRequestError, match="permanently closed"):
                        await ctx.session.get(Item, 1)
                    async with default_session_factory()() as connection:
                        assert (await connection.scalars(select(Item.name))).all() == ["visible"]
                    calls.append("verified")

                ctx.on_commit(calls.append, "outer")
                async with atomic():
                    ctx.on_commit(verify)
                ctx.on_commit(calls.append, "last")
                assert not calls
            assert calls == ["outer", "verified", "last"]
            with pytest.raises(ImproperlyConfigured, match="active unit of work"):
                ctx.on_commit(calls.append, "closed")

            async with runtime.session_factory.begin() as session:
                with pytest.raises(ImproperlyConfigured, match="active unit of work"):
                    Context(ANONYMOUS, session).on_commit(calls.append, "external transaction")

    asyncio.run(scenario())


def test_rollback_discards_callbacks_in_affected_savepoints() -> None:
    calls: list[str] = []

    async def scenario() -> None:
        async with configured():
            runtime = Obox(authenticated_app())._runtime
            async with runtime.uow(principal=ANONYMOUS) as ctx:
                ctx.on_commit(calls.append, "outer")
                with pytest.raises(ValueError, match="rollback parent"):  # noqa: PT012
                    async with atomic():
                        ctx.on_commit(calls.append, "parent")
                        async with ctx.session.begin_nested():
                            ctx.on_commit(calls.append, "released child")
                        raise ValueError("rollback parent")
                async with atomic():
                    ctx.on_commit(calls.append, "kept parent")
                    savepoint = await ctx.session.begin_nested()
                    ctx.on_commit(calls.append, "explicit rollback")
                    await savepoint.rollback()
                    ctx.on_commit(calls.append, "after rollback")
            assert calls == ["outer", "kept parent", "after rollback"]

    asyncio.run(scenario())


def test_outer_rollback_and_commit_failure_discard_callbacks() -> None:
    calls: list[str] = []

    async def scenario() -> None:
        async with configured():
            runtime = Obox(authenticated_app())._runtime
            with pytest.raises(ValueError, match="rollback"):  # noqa: PT012
                async with runtime.uow(principal=ANONYMOUS) as ctx:
                    ctx.on_commit(calls.append, "outer rollback")
                    raise ValueError("rollback")
            with pytest.raises(ResourceConflict, match="conflicts"):  # noqa: PT012
                async with runtime.uow(principal=ANONYMOUS) as ctx:
                    ctx.on_commit(calls.append, "failed commit")
                    ctx.session.add_all(
                        Item(name="duplicate", price=Decimal("2")) for _ in range(2)
                    )
            assert calls == []
            async with runtime.session_factory() as session:
                assert (await session.scalars(select(Item.name))).all() == []

    asyncio.run(scenario())


def test_rest_and_html_delete_share_after_commit_hook(caplog: pytest.LogCaptureFixture) -> None:
    deleted: list[int] = []

    async def confirm_delete(record_id: int) -> None:
        async with default_session_factory()() as connection:
            assert await connection.get(Item, record_id) is None
        deleted.append(record_id)

    def fail() -> None:
        raise RuntimeError("notification failed")

    class Deletions(ModelResource[Item]):
        model = Item

        async def perform_destroy(self, obj: Item) -> None:
            record_id = obj.id
            await super().perform_destroy(obj)
            self.on_commit(fail)
            self.on_commit(confirm_delete, record_id)

    async def scenario() -> None:
        async with configured():
            app = authenticated_app()
            runtime = Obox(app)
            runtime.register(Deletions, slug="items")
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://test",
                    headers={"x-test-user": "admin"},
                ) as client,
            ):
                async with default_session_factory().begin() as session:
                    session.add_all(
                        [
                            Item(id=1, name="rest", price=Decimal("2")),
                            Item(id=2, name="html", price=Decimal("2")),
                        ]
                    )
                assert (await client.delete("/v0/items/1")).status_code == 204
                assert deleted == [1]
                url = "/items/2/delete"
                page = await client.get(url)
                assert page.status_code == 200
                token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.text)
                assert token is not None
                assert (
                    await client.post(url, data={"csrfmiddlewaretoken": token[1]})
                ).status_code == 303
                assert deleted == [1, 2]
                assert caplog.messages.count("on_commit callback failed.") == 2

    asyncio.run(scenario())
