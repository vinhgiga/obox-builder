"""Owner-scoped JSON management and durable polling."""

from typing import Any
from uuid import UUID

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request, Response

from obox_builder.builder.api_schema import FieldError, Problem
from obox_builder.builder.filters import FilterGroup
from obox_builder.builder.http import ApiRoute
from obox_builder.builder.query import ListQuery, invalid, parameters
from obox_builder.context import Context
from obox_builder.contrib.webhooks.models import Delivery, Event, Subscription, now
from obox_builder.contrib.webhooks.schemas import (
    Deliveries,
    Enabled,
    SubscriptionCreated,
    SubscriptionInput,
    SubscriptionRead,
    Subscriptions,
)
from obox_builder.contrib.webhooks.services import actor, create, owned


class WebhookRoute(ApiRoute):
    cache_control = "private, no-store"


def build_router(plugin: Any) -> APIRouter:
    app = plugin.app
    router = APIRouter(
        prefix=app.settings.api_prefix + "/webhooks", route_class=WebhookRoute, tags=["webhooks"]
    )
    dependency = Depends(app.context_dependency("api"), scope="function")
    problem = Problem.model_json_schema()
    problem["properties"]["errors"]["items"] = FieldError.model_json_schema()
    problem.pop("$defs")
    router.responses.update(
        {
            status: {"content": {"application/problem+json": {"schema": problem}}}
            for status in (400, 401, 403, 404, 406, 409, 415, 422, 500)
        }
    )

    def mutation(request: Request, ctx: Context) -> None:
        parameters(request.query_params.multi_items())
        app.csrf.validate(request, ctx.principal)

    @router.post(
        "",
        status_code=201,
        response_model=SubscriptionCreated,
        name="obox_webhooks_create",
        operation_id="webhooks_create",
    )
    async def subscribe(
        request: Request, data: SubscriptionInput, ctx: Context = dependency
    ) -> Any:
        mutation(request, ctx)
        row = await create(plugin, data, ctx)
        return {
            **SubscriptionRead.model_validate(row).model_dump(),
            "secret": plugin.encoded_secret(row),
        }

    @router.get(
        "", response_model=Subscriptions, name="obox_webhooks_list", operation_id="webhooks_list"
    )
    async def subscriptions(request: Request, ctx: Context = dependency) -> Any:
        parameters(request.query_params.multi_items())
        return {
            "items": list(
                await ctx.session.scalars(
                    sa.select(Subscription)
                    .where(Subscription.owner_id == actor(ctx), Subscription.slot.is_not(None))
                    .order_by(Subscription.created_at, Subscription.id)
                )
            )
        }

    @router.patch(
        "/{id}",
        response_model=SubscriptionRead,
        name="obox_webhooks_update",
        operation_id="webhooks_update",
    )
    async def enabled(request: Request, id: UUID, data: Enabled, ctx: Context = dependency) -> Any:
        mutation(request, ctx)
        row = await owned(plugin, id, ctx)
        row.enabled = data.enabled
        return row

    @router.post(
        "/{id}:refresh",
        response_model=SubscriptionRead,
        name="obox_webhooks_refresh",
        operation_id="webhooks_refresh",
    )
    async def refresh(request: Request, id: UUID, ctx: Context = dependency) -> Any:
        mutation(request, ctx)
        row = await owned(plugin, id, ctx)
        row.expires_at = now() + plugin.subscription_ttl
        return row

    @router.delete(
        "/{id}", status_code=204, name="obox_webhooks_delete", operation_id="webhooks_delete"
    )
    async def delete(request: Request, id: UUID, ctx: Context = dependency) -> Response:
        mutation(request, ctx)
        if await request.body():
            raise invalid("body", "Request body must be empty.")
        row = await owned(plugin, id, ctx)
        row.enabled, row.slot = False, None
        return Response(status_code=204)

    @router.get(
        "/{id}/deliveries",
        response_model=Deliveries,
        name="obox_webhooks_deliveries",
        operation_id="webhooks_deliveries",
    )
    async def deliveries(request: Request, id: UUID, ctx: Context = dependency) -> Any:
        from obox_builder.contrib.webhooks.worker import authorized

        params = parameters(request.query_params.multi_items(), {"cursor", "limit"})
        query = ListQuery(params, FilterGroup())
        cursor, limit = query.integer("cursor", 0), query.integer("limit", 50)
        if limit is None or not 1 <= limit <= 100:
            raise invalid("limit", "Choose a limit from 1 to 100.")
        sub = await owned(plugin, id, ctx)
        rows = list(
            await ctx.session.execute(
                sa.select(Delivery, Event)
                .join(Event)
                .where(Delivery.subscription_id == sub.id, Delivery.sequence > cursor)
                .order_by(Delivery.sequence)
                .limit(limit + 1)
            )
        )
        items = []
        for delivery, event in rows[:limit]:
            cursor = delivery.sequence
            if await authorized(plugin, sub, event, ctx):
                items.append(
                    {
                        "id": delivery.id,
                        "sequence": delivery.sequence,
                        "event": event.payload,
                        "status": delivery.status,
                        "attempts": delivery.attempts,
                        "last_status": delivery.last_status,
                        "last_error": delivery.last_error,
                    }
                )
        return {"items": items, "next_cursor": cursor, "has_more": len(rows) > limit}

    return router
