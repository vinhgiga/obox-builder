"""Subscription ownership, transactional event fanout, and retention."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from obox.components.query import invalid
from obox.contrib.webhooks.models import Delivery, Event, Subscription, now
from obox.contrib.webhooks.schemas import ChangeNotification
from obox.contrib.webhooks.transport import destination
from obox.core.async_utils import invoke
from obox.core.exceptions import (
    ImproperlyConfigured,
    NotAuthenticated,
    NotFound,
    PermissionDenied,
    ResourceConflict,
)
from obox.runtime.context import Context


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def actor(ctx: Context) -> str:
    principal = ctx.principal
    if (
        not getattr(principal, "is_authenticated", False)
        or not getattr(principal, "is_active", False)
        or principal.id is None
    ):
        raise NotAuthenticated("An active authenticated identity is required.")
    if getattr(principal, "api_scopes", None) is not None:
        raise PermissionDenied("Bearer credentials cannot manage webhook subscriptions.")
    key = str(principal.id)
    if not key or len(key) > 255:
        raise PermissionDenied("The identity cannot own webhook subscriptions.")
    return key


def resource(plugin: Any, slug: str) -> Any:
    from obox.db.identity import model_label

    for model in plugin.models:
        if ".".join(model_label(model)) == slug:
            return model
    raise NotFound("The webhook model is unavailable.")


async def owned(plugin: Any, id: UUID, ctx: Context) -> Subscription:
    owner = actor(ctx)
    row = await ctx.session.scalar(
        sa.select(Subscription)
        .where(
            Subscription.id == id,
            Subscription.owner_id == owner,
            Subscription.slot.is_not(None),
        )
        .with_for_update()
    )
    if row is None:
        raise NotFound("The webhook subscription is unavailable.")
    return row


async def create(plugin: Any, data: Any, ctx: Context) -> Subscription:
    owner = actor(ctx)
    b = resource(plugin, data.resource)
    from obox.contrib._models import authorize

    authorize(b, ctx.principal, "view")
    if not await invoke(plugin.has_permission, b, ctx):
        raise PermissionDenied("This subscription is not permitted.")
    try:
        destination(data.url, plugin.allowed_hosts)
    except ValueError as exc:
        raise invalid("url", str(exc)) from exc
    if len(set(data.actions)) != len(data.actions):
        raise invalid("actions", "Choose unique change actions.")
    insert = {"sqlite": sqlite_insert, "postgresql": pg_insert}.get(
        ctx.session.get_bind().dialect.name
    )
    if insert is None:
        raise ImproperlyConfigured("Webhooks support SQLite and PostgreSQL.")
    values = {
        "id": uuid4(),
        "owner_id": owner,
        "resource": data.resource,
        "url": data.url,
        "actions": data.actions,
        "key_id": plugin.active_key,
        "nonce": secrets.token_hex(32),
        "enabled": True,
        "sequence": 0,
        "created_at": now(),
        "expires_at": now() + plugin.subscription_ttl,
    }
    for slot in range(1, plugin.max_subscriptions + 1):
        statement: Any = insert(Subscription).values(**values, slot=slot)
        row = (
            await ctx.session.scalars(
                statement.on_conflict_do_nothing(index_elements=["owner_id", "slot"]).returning(
                    Subscription
                )
            )
        ).one_or_none()
        if row is not None:
            return cast(Subscription, row)
    raise ResourceConflict("The webhook subscription maximum has been reached.")


async def record(plugin: Any, action: str, obj: Any, ctx: Context) -> None:
    from obox.db.identity import model_label

    if action not in {"create", "update"}:
        return
    model = type(obj)
    mapper = sa.inspect(model)
    pk_name = mapper.get_property_by_column(mapper.primary_key[0]).key
    slug = ".".join(model_label(model))
    timestamp = now()
    # Serialize allocation on each subscription. A polling cursor must never skip
    # a lower sequence that commits after a higher sequence.
    subscriptions = list(
        await ctx.session.scalars(
            sa.select(Subscription)
            .where(
                Subscription.resource == slug,
                Subscription.enabled.is_(True),
                Subscription.expires_at > timestamp,
                Subscription.slot.is_not(None),
            )
            .order_by(Subscription.id)
            .with_for_update()
        )
    )
    subscriptions = [sub for sub in subscriptions if action in sub.actions]
    if not subscriptions:
        return
    event_id = uuid4()
    payload = ChangeNotification.model_validate(
        {
            "id": event_id,
            "type": f"record.{action}d",
            "resource": slug,
            "action": action,
            "record_id": str(getattr(obj, pk_name)),
            "occurred_at": timestamp,
        }
    ).model_dump(mode="json", by_alias=False)
    ctx.session.add(
        Event(
            id=event_id,
            resource=slug,
            action=action,
            record_id=payload["record_id"],
            occurred_at=timestamp,
            payload=payload,
        )
    )
    await ctx.session.flush()
    for sub in subscriptions:
        sequence = await ctx.session.scalar(
            sa.update(Subscription)
            .where(Subscription.id == sub.id)
            .values(sequence=Subscription.sequence + 1)
            .returning(Subscription.sequence)
        )
        ctx.session.add(Delivery(subscription_id=sub.id, event_id=event_id, sequence=sequence))
    await ctx.session.flush()


async def prune(plugin: Any, ctx: Context) -> int:
    expired = sa.select(Event.id).where(Event.occurred_at < now() - plugin.retention)
    await ctx.session.execute(sa.delete(Delivery).where(Delivery.event_id.in_(expired)))
    ids = list(
        await ctx.session.scalars(sa.delete(Event).where(Event.id.in_(expired)).returning(Event.id))
    )
    return len(ids)
