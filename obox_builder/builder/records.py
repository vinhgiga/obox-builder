"""Read-only related cards over the core scoped CRUD statements."""

import hashlib
from typing import Any
from urllib.parse import quote

import sqlalchemy as sa
from pydantic import JsonValue
from sqlalchemy.orm import with_parent

from obox_builder.builder.api import Page, json_value
from obox_builder.builder.api_schema import ApiResponse
from obox_builder.builder.filters import FilterGroup
from obox_builder.builder.query import ListQuery, invalid
from obox_builder.builder.service import invoke
from obox_builder.context import Context
from obox_builder.errors import NotFound, PermissionDenied
from obox_builder.i18n import localize


class Card(ApiResponse):
    id: str
    label: str
    url: str | None
    values: dict[str, JsonValue]


class RelationshipPage(ApiResponse):
    field: str
    target_slug: str
    items: list[Card]
    page: Page


async def relationship(
    app: Any, builder: Any, id: str, field: str, params: dict[str, str], ctx: Context
) -> dict[str, Any]:
    obj = await app.service(builder.model).get(id, ctx=ctx)
    if field not in builder.view_related:
        raise NotFound("Relationship is unavailable.")
    allowed = builder.policy.read_fields(ctx, obj=obj)
    if allowed is not None and field not in allowed:
        raise PermissionDenied("Relationship is not readable.")
    relation = sa.inspect(builder.model).relationships[field]
    target = app._registrations.get(relation.mapper.class_)
    if target is None:
        raise NotFound("Relationship target is unavailable.")
    target = target.builder
    if ctx.source == "api":
        if not target.api.enabled or not {"list", "retrieve"}.issubset(target.api.operations):
            raise NotFound("Relationship target is unavailable.")
    elif not target.view.enabled:
        raise NotFound("Relationship target is unavailable.")
    service = app.service(target.model)
    await service.authorize("list", ctx)
    await service.authorize("retrieve", ctx)
    query = ListQuery(params, FilterGroup(), [(target.pk_name, False, False)])
    limit = query.integer(
        "limit", builder.api.default_limit if ctx.source == "api" else builder.view.page_size
    )
    if builder.api.max_limit is not None:
        if limit is not None and limit > builder.api.max_limit:
            raise invalid("limit", "Limit exceeds the configured maximum.")
        limit = builder.api.max_limit if limit is None else limit
    if "cursor" in params and not limit:
        raise invalid("cursor", "Cursor requires a positive limit.")
    binding = (
        str(ctx.principal.id),
        builder.route_key,
        id,
        field,
        limit,
        tuple(
            target.readable(ctx, fields=target.api_read if ctx.source == "api" else target.fields)
        ),
    )
    secret = hashlib.sha256(app.csrf.secret + repr(binding).encode()).digest()
    stmt = (
        service._statement("list", ctx)
        .where(
            target.policy.scope("retrieve", ctx), with_parent(obj, getattr(builder.model, field))
        )
        .distinct()
        .order_by(None)
        .order_by(target.pk)
    )
    if "cursor" in params:
        stmt = stmt.where(query.cursor(target, secret))
    items: list[dict[str, Any]] = []
    keys = []
    wanted = None if limit is None else max(1, limit + 1)
    while wanted is None or len(items) < wanted:
        batch = list((await ctx.session.scalars(stmt.limit(100))).unique())
        if not batch:
            break
        for child in batch:
            if not await invoke(target.policy.allow, "retrieve", ctx, obj=child):
                continue
            fields = target.readable(
                ctx, obj=child, fields=target.api_read if ctx.source == "api" else target.fields
            )
            allowed = target.policy.read_fields(ctx, obj=child)
            if ctx.source == "ui" and (allowed is None or target.pk_name in allowed):
                fields = (*fields, target.pk_name)
            if target.pk_name not in fields:
                continue
            names = [name for name in target.view_list_display if name in fields]
            row = await service.serialize(child, ctx=ctx, fields=names)
            key = getattr(child, target.pk_name)
            label = getattr(child, target.label_field) if target.label_field in fields else None
            url = f"{target.ui_path}/{quote(str(key), safe='')}" if target.view.enabled else None
            if url and ctx.request is not None:
                url = localize(ctx.request, url)
            items.append(
                {
                    "id": str(key),
                    "label": str(key if label is None or label == "" else label),
                    "url": url,
                    "values": {
                        target.api_names[name]: str(value)
                        if value is not None
                        and (
                            name in target.api_identifiers
                            or isinstance(target.specs[name].column.type, sa.BigInteger)
                        )
                        else json_value(value)
                        for name, value in row.items()
                    },
                }
            )
            keys.append(key)
            if wanted is not None and len(items) == wanted:
                break
        stmt = stmt.where(
            getattr(target.model, target.pk_name) > getattr(batch[-1], target.pk_name)
        )
    more = limit is not None and len(items) > limit
    return {
        "field": field,
        "target_slug": target.api_key,
        "items": items if limit is None else items[:limit],
        "page": {
            "limit": limit,
            "offset": 0,
            "has_more": more,
            "total": None,
            "next_cursor": query.cursor(target, secret, [keys[limit - 1]])
            if more and limit
            else None,
        },
    }
