"""Short database leases, fenced completion, and at-least-once HTTPS delivery."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import secrets
from contextlib import suppress
from datetime import timedelta
from importlib.util import find_spec
from typing import Any, cast
from uuid import uuid4

import sqlalchemy as sa

from obox_builder.builder.service import invoke
from obox_builder.context import Context
from obox_builder.contrib.webhooks.models import Delivery, Event, Subscription, now
from obox_builder.contrib.webhooks.schemas import ChangeNotification
from obox_builder.contrib.webhooks.services import aware, resource
from obox_builder.contrib.webhooks.transport import send
from obox_builder.errors import ImproperlyConfigured, OboxError

logger = logging.getLogger(__name__)


async def authorized(plugin: Any, sub: Subscription, event: Event, ctx: Context) -> bool:
    principal = ctx.principal
    if (
        principal is None
        or not getattr(principal, "is_authenticated", False)
        or not getattr(principal, "is_active", False)
        or str(principal.id) != sub.owner_id
    ):
        return False
    try:
        b = resource(plugin, sub.resource)
        obj = await plugin.app.service(b.model).get(event.record_id, ctx=ctx)
        return b.pk_name in b.readable(ctx, obj=obj, fields=b.api_read)
    except OboxError:
        return False


class Worker:
    def __init__(self, plugin: Any, *, transport: Any = None) -> None:
        if find_spec("httpx") is None:
            raise ImproperlyConfigured("Install obox-builder[webhooks] to run a webhook worker.")
        self.plugin, self.transport = plugin, transport

    async def claim(self) -> Delivery | None:
        plugin = self.plugin
        timestamp = now()
        eligible = sa.or_(
            sa.and_(Delivery.status.in_(("pending", "retry")), Delivery.due_at <= timestamp),
            sa.and_(Delivery.status == "sending", Delivery.locked_until <= timestamp),
        )
        async with plugin.app.session_factory(expire_on_commit=False) as db, db.begin():
            await db.execute(
                sa.update(Delivery)
                .where(eligible, Delivery.attempts >= plugin.max_attempts)
                .values(
                    status="failed",
                    lock_token=None,
                    locked_until=None,
                    completed_at=timestamp,
                    last_error="attempts_exhausted",
                )
            )
            candidate = sa.select(Delivery.id).where(
                eligible, Delivery.attempts < plugin.max_attempts
            )
            candidate_id = (
                candidate.order_by(Delivery.due_at, Delivery.id)
                .limit(1)
                .with_for_update(skip_locked=True)
                .scalar_subquery()
            )
            row = (
                await db.scalars(
                    sa.update(Delivery)
                    .where(Delivery.id == candidate_id, eligible)
                    .values(
                        status="sending",
                        attempts=Delivery.attempts + 1,
                        lock_token=uuid4(),
                        locked_until=timestamp + timedelta(seconds=60),
                    )
                    .returning(Delivery)
                )
            ).one_or_none()
            return cast(Delivery | None, row)

    async def deliver(self, delivery: Delivery) -> tuple[str, int | None, str | None]:
        plugin = self.plugin
        async with asyncio.timeout(30):
            async with plugin.app.uow(principal=None, source="api") as ctx:
                sub = await ctx.session.get(Subscription, delivery.subscription_id)
                event = await ctx.session.get(Event, delivery.event_id)
                if (
                    sub is None
                    or event is None
                    or sub.slot is None
                    or not sub.enabled
                    or aware(sub.expires_at) <= now()
                    or aware(event.occurred_at) + plugin.retention <= now()
                ):
                    return "skipped", None, "subscription_unavailable"
                ctx.principal = await invoke(plugin.resolve_principal, sub.owner_id, ctx)
                if not await authorized(plugin, sub, event, ctx):
                    return "skipped", None, "permission_revoked"
                body = (
                    ChangeNotification.model_validate(event.payload)
                    .model_dump_json(by_alias=True)
                    .encode()
                )
                timestamp = str(int(now().timestamp()))
                # A stable delivery ID deduplicates retries; every attempt gets
                # a fresh timestamp and signature over the exact transmitted bytes.
                message = f"{delivery.id}.{timestamp}.".encode() + body
                signature = base64.b64encode(
                    hmac.digest(plugin.secret(sub), message, hashlib.sha256)
                ).decode()
                url = sub.url
            code = await send(
                url,
                body,
                {
                    "Content-Type": "application/json",
                    "webhook-id": str(delivery.id),
                    "webhook-timestamp": timestamp,
                    "webhook-signature": "v1," + signature,
                },
                allowed_hosts=plugin.allowed_hosts,
                transport=self.transport,
            )
            if 200 <= code < 300:
                return "delivered", code, None
            if code in {408, 429} or code >= 500:
                return "retry", code, "http_error"
            return "failed", code, "http_error"

    async def run_once(self) -> bool:
        delivery = await self.claim()
        if delivery is None:
            return False
        try:
            status, code, error = await self.deliver(delivery)
        except ValueError:
            status, code, error = "failed", None, "destination_blocked"
        except Exception:  # noqa: BLE001 - host identity resolvers are an external boundary
            logger.warning("Webhook delivery failed: %s", delivery.id)
            status, code, error = "retry", None, "delivery_error"
        if status == "retry" and delivery.attempts >= self.plugin.max_attempts:
            status = "failed"
        timestamp = now()
        delay = secrets.randbelow(min(3600, 30 * 2 ** min(delivery.attempts - 1, 7)) + 1)
        async with self.plugin.app.session_factory.begin() as db:
            await db.execute(
                sa.update(Delivery)
                .where(
                    Delivery.id == delivery.id,
                    Delivery.status == "sending",
                    Delivery.lock_token == delivery.lock_token,
                    Delivery.locked_until > timestamp,
                )
                .values(
                    status=status,
                    last_status=code,
                    last_error=error,
                    due_at=timestamp + timedelta(seconds=delay),
                    lock_token=None,
                    locked_until=None,
                    completed_at=None if status == "retry" else timestamp,
                )
            )
        return True

    async def run(self, stop: asyncio.Event | None = None) -> None:
        stop = stop if stop is not None else asyncio.Event()
        while not stop.is_set():
            if not await self.run_once():
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=1)
