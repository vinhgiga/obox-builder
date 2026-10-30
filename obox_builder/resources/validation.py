"""Relation visibility and uniqueness checks shared by all operation callers."""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa

from obox_builder.resources.context import OperationContext
from obox_builder.resources.registry import get_registry


async def relation_rows(ctx: OperationContext, spec: Any, values: list[Any]) -> list[Any]:
    if not values:
        return []
    registry = ctx.registry or get_registry()
    try:
        resource = registry.for_model(spec.options["target"])
    except LookupError:
        return []
    context = ctx.for_resource(resource)
    if not resource.can_execute(context, "list"):
        return []
    key = spec.options.get("target_field") or resource.pk_column().key
    stmt = resource.scoped_query(context, resource.operations["list"]).where(
        getattr(resource.model, key).in_(values)
    )
    return [
        row
        for row in (await ctx.session.scalars(stmt)).unique()
        if resource.can_execute(context, "get", row)
    ]


async def validate_relations(resource: Any, ctx: OperationContext, bound: Any) -> None:
    for spec in bound.specs:
        if spec.type.name not in {"link", "multi_link"} or spec.name not in bound.cleaned:
            continue
        value = bound.cleaned[spec.name]
        values = list(dict.fromkeys(value)) if spec.type.name == "multi_link" else [value]
        values = [item for item in values if item is not None]
        if len(await relation_rows(ctx, spec, values)) != len(values):
            bound.errors[spec.name] = ["Select a valid choice."]
            bound.parsed = None


async def save_relations(resource: Any, ctx: OperationContext, obj: Any, bound: Any) -> None:
    from obox_builder.exceptions import PermissionDenied

    for spec in bound.specs:
        if spec.type.name != "multi_link" or spec.name not in bound.cleaned:
            continue
        values = bound.cleaned[spec.name]
        rows = await relation_rows(ctx, spec, values)
        if len(rows) != len(values):
            raise PermissionDenied("A selected record is no longer available.")
        if spec.name in sa.inspect(obj).unloaded:
            await ctx.session.refresh(obj, [spec.name])
        setattr(obj, spec.name, rows)


async def validate_unique(model: Any, ctx: OperationContext, bound: Any, obj: Any = None) -> None:
    from obox_builder.resources.uniqueness import validate_model_uniqueness

    await validate_model_uniqueness(ctx, model, bound, obj)
