"""Permission-scoped relation choices, validation and display."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from obox_builder.contrib.admin.context import AdminContext
from obox_builder.contrib.admin.options import ModelAdmin
from obox_builder.fields import Cell, FieldSpec
from obox_builder.forms import BoundForm


async def related_objects(
    ctx: AdminContext,
    spec: FieldSpec,
    *,
    pks: Sequence[Any] | None = None,
    limit: int | None = None,
) -> list[Any]:
    target = spec.options.get("target")
    if target is None:
        return []
    admin = ctx.site.get_model_admin_for(target) or ModelAdmin(target, ctx.site)
    related_ctx = replace(ctx, admin=admin, obj=None)
    if not admin.has_view_permission(related_ctx):
        return []
    stmt = admin.get_queryset(related_ctx).order_by(None).order_by(admin.pk_column())
    if pks is not None:
        stmt = stmt.where(admin.pk_column().in_(pks))
    if limit is not None:
        stmt = stmt.limit(limit)
    objects = (await ctx.db.scalars(stmt)).unique().all()
    return [obj for obj in objects if admin.has_view_permission(replace(related_ctx, obj=obj), obj)]


async def validate_relations(ctx: AdminContext, bound: BoundForm) -> None:
    for spec in bound.specs:
        value = bound.cleaned.get(spec.name)
        if (
            spec.type.name == "link"
            and value is not None
            and not await related_objects(ctx, spec, pks=[value])
        ):
            bound.errors[spec.name] = ["Select a valid choice."]


async def build_cells(
    ctx: AdminContext, admin: ModelAdmin, specs: Sequence[FieldSpec], objects: Sequence[Any]
) -> list[list[Cell]]:
    allowed: dict[str, set[Any]] = {}
    for spec in specs:
        target = spec.options.get("target")
        if spec.type.name != "link" or spec.column is None or target is None:
            continue
        pks = list({getattr(obj, spec.column.key or spec.name, None) for obj in objects} - {None})
        rows = await related_objects(ctx, spec, pks=pks) if pks else []
        target_admin = ctx.site.get_model_admin_for(target) or ModelAdmin(target, ctx.site)
        allowed[spec.name] = {target_admin.get_pk(obj) for obj in rows}
    return [
        [
            Cell()
            if spec.secret
            or (
                spec.name in allowed
                and spec.column is not None
                and getattr(obj, spec.column.key or spec.name, None) not in allowed[spec.name]
            )
            else admin.build_cell(spec, obj)
            for spec in specs
        ]
        for obj in objects
    ]
