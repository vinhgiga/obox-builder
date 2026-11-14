from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import os
import socket
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from types import ModuleType
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI, Request
from obox_builder import Context, ModelBuilder, Obox, Principal, Settings
from obox_builder.auth import RBAC, ScopedIdentity
from obox_builder.contrib.webhooks import Webhooks
from obox_builder.contrib.webhooks.cli import run
from obox_builder.contrib.webhooks.models import Base as WebhookBase
from obox_builder.contrib.webhooks.models import Delivery, Event, Subscription, now
from obox_builder.contrib.webhooks.transport import destination, resolve
from obox_builder.contrib.webhooks.worker import Worker
from obox_builder.errors import ImproperlyConfigured, PermissionDenied
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Record(Base):
    __tablename__ = "webhook_records"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(40), unique=True)


STAFF = Principal(id=1, is_authenticated=True, is_staff=True, is_superuser=True)
OTHER = Principal(id=2, is_authenticated=True, is_staff=True, is_superuser=True)


class Policy(RBAC[Any]):
    hide_identity = False

    def scope(self, action: str, ctx: Context) -> Any:
        return Record.name != "hidden" if action == "retrieve" else sa.true()

    async def allow(self, action: str, ctx: Context, *, obj: Any = None) -> bool:
        return (obj is None or obj.name != "denied") and await super().allow(action, ctx, obj=obj)

    def read_fields(self, ctx: Context, *, obj: Any = None) -> frozenset[str] | None:
        return frozenset({"name"}) if self.hide_identity else None


class Resource(ModelBuilder[Record]):
    slug = "records"
    fields = ("id", "name")

    async def before_save(
        self, instance: Record, changes: Any, ctx: Context, *, created: bool
    ) -> None:
        if instance.name == "reject":
            raise PermissionDenied("Rejected by hook.")


@asynccontextmanager
async def application(
    path: Path, **options: Any
) -> AsyncIterator[tuple[Obox, httpx.AsyncClient, Webhooks, Policy]]:
    engine = create_async_engine(
        os.getenv("OBOX_TEST_DATABASE_URL", f"sqlite+aiosqlite:///{path / 'webhooks.sqlite3'}")
    )
    namespace = "webhooks_" + uuid4().hex if engine.dialect.name == "postgresql" else None
    if namespace:
        async with engine.begin() as conn:
            await conn.execute(sa.schema.CreateSchema(namespace))
        engine = engine.execution_options(schema_translate_map={None: namespace})
    async with engine.begin() as conn:
        for metadata in (Base.metadata, WebhookBase.metadata):
            await conn.run_sync(metadata.create_all)

    def principal(request: Request) -> Any:
        actor = request.headers.get("actor", "1")
        if actor == "anonymous":
            return Principal()
        if actor == "bearer":
            return ScopedIdentity(STAFF, frozenset({"admin.records.retrieve"}))
        return OTHER if actor == "2" else STAFF

    async def current(owner: str, ctx: Context) -> Any:
        return app.app.state.identities.get(owner)

    plugin = Webhooks(
        signing_keys={"primary": b"k" * 32},
        active_key="primary",
        resolve_principal=current,
        allowed_hosts=("receiver.example",),
        **options,
    )
    policy = Policy()
    app = Obox(
        FastAPI(),
        settings=Settings(
            session_factory=async_sessionmaker(engine, expire_on_commit=False),
            principal_dependency=principal,
            default_policy=policy,
            csrf_secret="x" * 32,
        ),
        plugins=[plugin],
    )
    app.app.state.identities = {"1": STAFF, "2": OTHER}
    try:
        app.register(Resource)
        policy = cast(Policy, app.get_builder(Record).policy)
        async with (
            app.app.router.lifespan_context(app.app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app.app), base_url="http://test"
            ) as client,
        ):
            yield app, client, plugin, policy
    finally:
        if namespace:
            async with engine.begin() as conn:
                await conn.execute(sa.schema.DropSchema(namespace, cascade=True))
        await engine.dispose()


async def subscribe(client: httpx.AsyncClient, **kwargs: Any) -> httpx.Response:
    return await client.post(
        "/api/webhooks",
        json={"resource": "records", "url": "https://receiver.example/events", **kwargs},
    )


@pytest.fixture
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )


def test_subscription_ownership_secrets_expiry_filters_and_deletion(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with application(tmp_path, max_subscriptions=1) as (app, client, plugin, _):
            result = await subscribe(client, actions=["create"])
            assert result.status_code == 201, result.text
            sub = result.json()
            path = f"/api/webhooks/{sub['id']}"
            assert sub["secret"].startswith("whsec_")
            assert (await subscribe(client)).status_code == 409
            listed = await client.get("/api/webhooks")
            assert listed.headers["cache-control"] == "private, no-store"
            assert len(listed.json()["items"]) == 1
            assert set(listed.json()["items"][0]) == {
                "id",
                "resource",
                "url",
                "actions",
                "enabled",
                "createdAt",
                "expiresAt",
            }
            cases: list[tuple[str, str, dict[str, Any]]] = [
                ("DELETE", "", {}),
                ("PATCH", "", {"json": {"enabled": False}}),
                ("POST", ":refresh", {}),
                ("GET", "/deliveries", {}),
            ]
            for method, suffix, kwargs in cases:
                assert (
                    await client.request(method, path + suffix, headers={"actor": "2"}, **kwargs)
                ).status_code == 404
            assert (await client.get("/api/webhooks", headers={"actor": "2"})).json() == {
                "items": []
            }
            assert (await client.patch(path, json={"enabled": False})).status_code == 200
            await client.post("/api/records", json={"name": "disabled"})
            assert (await client.get(path + "/deliveries")).json()["items"] == []
            assert (await client.patch(path, json={"enabled": True})).status_code == 200
            async with app.uow(principal=STAFF) as ctx:
                await ctx.session.execute(
                    sa.update(Subscription).values(expires_at=now() - timedelta(seconds=1))
                )
            await client.post("/api/records", json={"name": "expired"})
            assert (await client.post(path + ":refresh")).status_code == 200
            created = await client.post("/api/records", json={"name": "visible"})
            await client.patch(f"/api/records/{created.json()['id']}", json={"name": "updated"})
            page = (await client.get(path + "/deliveries")).json()
            assert len(page["items"]) == 1
            assert page["items"][0]["event"]["action"] == "create"
            assert (await client.delete(path)).status_code == 204
            assert (await client.get("/api/webhooks")).json() == {"items": []}
            assert (await subscribe(client)).status_code == 201
            assert plugin.secret(await _subscription(app, sub["id"])) == base64.b64decode(
                sub["secret"][6:]
            )

    asyncio.run(scenario())


async def _subscription(app: Obox, id: str) -> Subscription:
    async with app.uow(principal=STAFF) as ctx:
        row = await ctx.session.get(Subscription, UUID(id))
        assert row is not None
        return row


def test_atomic_outbox_polling_and_no_delete_events(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with application(tmp_path) as (app, client, _, _):
            sub = (await subscribe(client)).json()
            url = f"/api/webhooks/{sub['id']}/deliveries"
            failed = await client.post(
                "/api/records:batchWrite",
                json={
                    "operations": [
                        {"op": "create", "data": {"name": "rolled-back"}},
                        {"op": "create", "data": {"name": "reject"}},
                    ]
                },
            )
            assert failed.status_code == 403
            assert (await client.get(url)).json()["items"] == []
            async with app.uow(principal=STAFF) as ctx:
                assert await ctx.session.scalar(sa.select(sa.func.count()).select_from(Event)) == 0
            created = await client.post("/api/records", json={"name": "one"})
            assert created.status_code == 201, created.text
            path = f"/api/records/{created.json()['id']}"
            assert (await client.patch(path, json={"name": "two"})).status_code == 200
            first = (await client.get(url, params={"limit": 1})).json()
            assert first["hasMore"] is True
            event = first["items"][0]["event"]
            assert set(event) == {
                "id",
                "version",
                "type",
                "resource",
                "action",
                "recordId",
                "occurredAt",
            }
            assert event["type"] == "record.created"
            second = (await client.get(url, params={"cursor": first["nextCursor"]})).json()
            assert second["hasMore"] is False
            assert second["items"][0]["event"]["type"] == "record.updated"
            assert second["nextCursor"] > first["nextCursor"]
            assert (await client.delete(path)).status_code == 204
            # Deleted records are no longer readable, including their prior events.
            assert (await client.get(url)).json()["items"] == []
            async with app.uow(principal=STAFF) as ctx:
                assert await ctx.session.scalar(sa.select(sa.func.count()).select_from(Event)) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "body",
    [
        {"url": "http://receiver.example/events"},
        {"url": "https://other.example/events"},
        {"url": "https://user:password@receiver.example/events"},
        {"url": "https://receiver.example:8443/events"},
        {"actions": ["delete"]},
        {"actions": ["create", "create"]},
        {"actions": []},
        {"resource": "unknown"},
    ],
)
def test_subscription_input_validation(tmp_path: Path, body: dict[str, Any]) -> None:
    async def scenario() -> None:
        async with application(tmp_path) as (_, client, _, _):
            result = await subscribe(client, **body)
            assert result.status_code == (404 if body.get("resource") == "unknown" else 422), (
                result.text
            )

    asyncio.run(scenario())


def test_subscription_authentication_csrf_and_no_token_delegation(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with application(tmp_path) as (_, client, _, _):
            for actor, status in [("anonymous", 401), ("bearer", 403)]:
                result = await client.post(
                    "/api/webhooks",
                    headers={"actor": actor},
                    json={"resource": "records", "url": "https://receiver.example/events"},
                )
                assert result.status_code == status, result.text
            client.cookies.set("session", "invalid")
            assert (await subscribe(client)).status_code == 403

    asyncio.run(scenario())


@pytest.mark.usefixtures("public_dns")
def test_delivery_signature_retry_and_pinned_destination(tmp_path: Path) -> None:
    async def scenario() -> None:
        requests: list[httpx.Request] = []

        def receive(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(503 if len(requests) == 1 else 204)

        async with application(tmp_path) as (app, client, plugin, _):
            sub = (await subscribe(client)).json()
            await client.post("/api/records", json={"name": "private body"})
            worker = Worker(plugin, transport=httpx.MockTransport(receive))
            assert await worker.run_once()
            url = f"/api/webhooks/{sub['id']}/deliveries"
            pending = (await client.get(url)).json()["items"][0]
            assert pending["status"] == "retry"
            async with app.uow(principal=STAFF) as ctx:
                await ctx.session.execute(
                    sa.update(Delivery).values(due_at=now() - timedelta(seconds=1))
                )
            assert await worker.run_once()
            assert not await worker.run_once()
            done = (await client.get(url)).json()["items"][0]
            assert done["status"] == "delivered"
            assert done["attempts"] == 2
            for request in requests:
                assert request.url.host == "93.184.216.34"
                assert request.headers["host"] == "receiver.example"
                assert request.extensions["sni_hostname"] == "receiver.example"
                assert b"private body" not in request.content
                signed = (
                    f"{request.headers['webhook-id']}.{request.headers['webhook-timestamp']}.".encode()
                    + request.content
                )
                expected = hmac.digest(base64.b64decode(sub["secret"][6:]), signed, hashlib.sha256)
                assert (
                    request.headers["webhook-signature"]
                    == "v1," + base64.b64encode(expected).decode()
                )
            assert requests[0].headers["webhook-id"] == requests[1].headers["webhook-id"]
            assert requests[0].content == requests[1].content

    asyncio.run(scenario())


@pytest.mark.parametrize("revocation", ["identity", "scope", "object", "fields", "disabled"])
@pytest.mark.usefixtures("public_dns")
def test_worker_and_polling_recheck_current_access(tmp_path: Path, revocation: str) -> None:
    async def scenario() -> None:
        sent: list[httpx.Request] = []
        async with application(tmp_path) as (app, client, plugin, policy):
            assert (await subscribe(client)).status_code == 201
            row = await client.post("/api/records", json={"name": "one"})
            async with app.uow(principal=STAFF) as ctx:
                if revocation == "identity":
                    app.app.state.identities.clear()
                elif revocation in {"scope", "object"}:
                    await ctx.session.execute(
                        sa.update(Record).values(
                            name="hidden" if revocation == "scope" else "denied"
                        )
                    )
                elif revocation == "fields":
                    policy.hide_identity = True
                else:
                    await ctx.session.execute(sa.update(Subscription).values(enabled=False))

            def receive(request: httpx.Request) -> httpx.Response:
                sent.append(request)
                return httpx.Response(200)

            assert await Worker(plugin, transport=httpx.MockTransport(receive)).run_once()
            assert sent == []
            async with app.uow(principal=STAFF) as ctx:
                delivery = (await ctx.session.scalars(sa.select(Delivery))).one()
                assert delivery.status == "skipped"
            assert row.status_code == 201

    asyncio.run(scenario())


@pytest.mark.parametrize("code", [301, 400, 408, 429, 500])
@pytest.mark.usefixtures("public_dns")
def test_terminal_delivery_errors_and_attempt_limits(tmp_path: Path, code: int) -> None:
    async def scenario() -> None:
        async with application(tmp_path, max_attempts=1) as (app, client, plugin, _):
            await subscribe(client)
            await client.post("/api/records", json={"name": "one"})
            requests = []

            def receive(request: httpx.Request) -> httpx.Response:
                requests.append(request)
                return httpx.Response(code, headers={"location": "https://127.0.0.1/"})

            worker = Worker(plugin, transport=httpx.MockTransport(receive))
            assert await worker.run_once()
            assert not await worker.run_once()
            assert len(requests) == 1
            async with app.uow(principal=STAFF) as ctx:
                delivery = (await ctx.session.scalars(sa.select(Delivery))).one()
                assert delivery.status == "failed"
                assert delivery.last_status == code

    asyncio.run(scenario())


def test_competing_workers_and_expired_lease_recovery(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with application(tmp_path) as (app, client, plugin, _):
            await subscribe(client)
            await client.post("/api/records", json={"name": "one"})
            worker = Worker(plugin)
            claims = await asyncio.gather(worker.claim(), worker.claim())
            claimed = [claim for claim in claims if claim is not None]
            assert len(claimed) == 1
            old = claimed[0]
            async with app.uow(principal=STAFF) as ctx:
                await ctx.session.execute(
                    sa.update(Delivery).values(locked_until=now() - timedelta(seconds=1))
                )
            replacement = await worker.claim()
            assert replacement is not None
            assert replacement.id == old.id
            assert replacement.lock_token != old.lock_token
            assert replacement.attempts == 2

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1", "224.0.0.1"]
)
def test_ssrf_blocks_private_and_special_addresses(
    monkeypatch: pytest.MonkeyPatch, address: str
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))],
    )
    with pytest.raises(ValueError, match="public"):
        asyncio.run(resolve("https://receiver.example/events"))


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "https://user@receiver.example/",
        "https://receiver.example/#fragment",
        "https://receiver.example:22/",
        "https://receiver.example\\@127.0.0.1/",
        "https://receiver.example/\nsecret",
    ],
)
def test_destination_validation(url: str) -> None:
    with pytest.raises(ValueError, match="public HTTPS"):
        destination(url)


def test_retention_and_signing_key_configuration(tmp_path: Path) -> None:
    for keys in ({}, {"key": b"short"}):
        with pytest.raises(ImproperlyConfigured):
            Webhooks(signing_keys=keys, active_key="key", resolve_principal=lambda *_: STAFF)

    async def scenario() -> None:
        async with application(tmp_path) as (app, client, plugin, _):
            await subscribe(client)
            await client.post("/api/records", json={"name": "one"})
            async with app.uow(principal=STAFF) as ctx:
                await ctx.session.execute(
                    sa.update(Event).values(occurred_at=now() - timedelta(days=8))
                )
                assert await plugin.prune(ctx) == 1
            async with app.uow(principal=STAFF) as ctx:
                assert (
                    await ctx.session.scalar(sa.select(sa.func.count()).select_from(Delivery)) == 0
                )

    asyncio.run(scenario())


def test_concurrent_subscription_limits_and_fanout_cursors(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with application(tmp_path, max_subscriptions=1) as (_, client, _, _):
            responses = await asyncio.gather(subscribe(client), subscribe(client))
            assert sorted(response.status_code for response in responses) == [201, 409]
            subscription = next(
                response.json() for response in responses if response.status_code == 201
            )
            rows = await asyncio.gather(
                *(client.post("/api/records", json={"name": name}) for name in ("one", "two"))
            )
            assert [row.status_code for row in rows] == [201, 201]
            url = f"/api/webhooks/{subscription['id']}/deliveries"
            first = (await client.get(url, params={"limit": 1})).json()
            second = (await client.get(url, params={"cursor": first["nextCursor"]})).json()
            assert first["items"][0]["sequence"] == 1
            assert second["items"][0]["sequence"] == 2

    asyncio.run(scenario())


def test_lost_lease_cannot_overwrite_replacement_completion(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with application(tmp_path) as (app, client, plugin, _):
            await subscribe(client)
            await client.post("/api/records", json={"name": "one"})
            started, finish = asyncio.Event(), asyncio.Event()

            class Delayed(Worker):
                async def deliver(self, delivery: Delivery) -> tuple[str, int | None, str | None]:
                    started.set()
                    await finish.wait()
                    return "failed", 400, "old_attempt"

            class Replacement(Worker):
                async def deliver(self, delivery: Delivery) -> tuple[str, int | None, str | None]:
                    return "delivered", 204, None

            running = asyncio.create_task(Delayed(plugin).run_once())
            await asyncio.wait_for(started.wait(), timeout=5)
            async with app.uow(principal=STAFF) as ctx:
                await ctx.session.execute(
                    sa.update(Delivery).values(locked_until=now() - timedelta(seconds=1))
                )
            assert await Replacement(plugin).run_once()
            finish.set()
            assert await running
            async with app.uow(principal=STAFF) as ctx:
                delivery = (await ctx.session.scalars(sa.select(Delivery))).one()
                assert delivery.status == "delivered"
                assert delivery.last_status == 204
                assert delivery.attempts == 2

    asyncio.run(scenario())


@pytest.mark.usefixtures("public_dns")
@pytest.mark.parametrize("failure", ["network", "blocked", "resolver"])
def test_external_failures_are_bounded_and_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    async def scenario() -> None:
        async with application(tmp_path) as (app, client, plugin, _):
            await subscribe(client)
            await client.post("/api/records", json={"name": "one"})
            if failure == "blocked":
                monkeypatch.setattr(
                    socket,
                    "getaddrinfo",
                    lambda *args, **kwargs: [
                        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
                    ],
                )
            if failure == "resolver":

                def broken(*args: Any) -> Any:
                    raise RuntimeError("sensitive identity details")

                plugin.resolve_principal = broken

            def receive(request: httpx.Request) -> httpx.Response:
                raise httpx.ConnectError("sensitive network details", request=request)

            assert await Worker(plugin, transport=httpx.MockTransport(receive)).run_once()
            async with app.uow(principal=STAFF) as ctx:
                delivery = (await ctx.session.scalars(sa.select(Delivery))).one()
                assert delivery.status == ("failed" if failure == "blocked" else "retry")
                assert delivery.last_error in {"destination_blocked", "delivery_error"}

    asyncio.run(scenario())


def test_abandoned_final_attempt_is_terminal_and_stop_is_respected(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with application(tmp_path, max_attempts=1) as (app, client, plugin, _):
            await subscribe(client)
            await client.post("/api/records", json={"name": "one"})
            worker = Worker(plugin)
            assert await worker.claim() is not None
            async with app.uow(principal=STAFF) as ctx:
                await ctx.session.execute(
                    sa.update(Delivery).values(locked_until=now() - timedelta(seconds=1))
                )
            assert not await worker.run_once()
            stop = asyncio.Event()
            stop.set()
            await worker.run(stop)
            async with app.uow(principal=STAFF) as ctx:
                delivery = (await ctx.session.scalars(sa.select(Delivery))).one()
                assert delivery.status == "failed"
                assert delivery.last_error == "attempts_exhausted"

    asyncio.run(scenario())


def test_worker_cli_loads_factory_and_prunes_with_host_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        async with application(tmp_path) as (app, client, _, _):
            await subscribe(client)
            await client.post("/api/records", json={"name": "one"})
            async with app.uow(principal=STAFF) as ctx:
                await ctx.session.execute(
                    sa.update(Event).values(occurred_at=now() - timedelta(days=8))
                )
            module = ModuleType("_obox_webhook_cli_test")
            created: list[Obox] = []

            def factory() -> Any:
                plugin = Webhooks(
                    signing_keys={"v1": b"k" * 32},
                    active_key="v1",
                    resolve_principal=lambda *_: STAFF,
                )
                obox = Obox(
                    FastAPI(),
                    settings=Settings(
                        session_factory=app.session_factory,
                        csrf_secret="x" * 32,
                        default_policy=Policy(),
                        principal_dependency=lambda: STAFF,
                    ),
                    plugins=[plugin],
                )
                obox.register(Resource)
                created.append(obox)
                return obox.app

            vars(module)["factory"] = factory
            monkeypatch.setitem(sys.modules, module.__name__, module)
            for action in ("prune", "worker"):
                await run(
                    argparse.Namespace(
                        app=module.__name__ + ":factory", factory=True, action=action, once=True
                    )
                )
            assert len(created) == 2
            assert all(obox._frozen and not obox.database._active for obox in created)
            async with app.uow(principal=STAFF) as ctx:
                assert await ctx.session.scalar(sa.select(sa.func.count()).select_from(Event)) == 0

    asyncio.run(scenario())
