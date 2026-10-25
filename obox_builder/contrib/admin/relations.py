"""Permission-scoped relation choices, validation and display."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Any

import sqlalchemy as sa

from obox_builder.contrib.admin.context import AdminContext
from obox_builder.contrib.admin.options import ModelAdmin
from obox_builder.core.exceptions import PermissionDenied
from obox_builder.fields import Cell, FieldSpec
from obox_builder.forms import BoundForm


def related_admin(ctx: AdminContext, spec: FieldSpec) -> tuple[ModelAdmin, AdminContext]:
    target = spec.options["target"]
    admin = ctx.site.get_model_admin_for(target) or ModelAdmin(target, ctx.site)
    return admin, replace(ctx, admin=admin, obj=None)


def related_key(spec: FieldSpec) -> Any:
    mapper = sa.inspect(spec.options["target"])
    name = spec.options.get("target_field")
    return mapper.columns[str(name)] if name else mapper.primary_key[0]


async def related_objects(
    ctx: AdminContext,
    spec: FieldSpec,
    *,
    pks: Sequence[Any] | None = None,
    limit: int | None = None,
) -> list[Any]:
    if spec.options.get("target") is None:
        return []
    admin, related_ctx = related_admin(ctx, spec)
    if not admin.has_view_permission(related_ctx):
        return []
    stmt = admin.get_queryset(related_ctx).order_by(None).order_by(admin.pk_column())
    if pks is not None:
        stmt = stmt.where(related_key(spec).in_(pks))
    if limit is not None:
        stmt = stmt.limit(limit)
    objects = (await ctx.db.scalars(stmt)).unique().all()
    return [obj for obj in objects if admin.has_view_permission(replace(related_ctx, obj=obj), obj)]


def input_values(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    return [str(item) for item in value] if isinstance(value, (list, tuple)) else [str(value)]


async def relation_choices(
    ctx: AdminContext, spec: FieldSpec, selected: Any, *, autocomplete: bool = False
) -> list[tuple[str, str]]:
    key = related_key(spec)
    values = input_values(selected)
    rows = await related_objects(ctx, spec, limit=500) if not autocomplete else []
    choices = [(str(getattr(row, key.key)), _label(row, key.key)) for row in rows]
    missing = set(values) - {value for value, _text in choices}
    if missing:
        try:
            pks = [key.type.python_type(value) for value in missing]
        except (ValueError, TypeError):
            pks = []
        rows = await related_objects(ctx, spec, pks=pks) if pks else []
        labels = {str(getattr(row, key.key)): _label(row, key.key) for row in rows}
        choices.extend(
            (value, labels.get(value, "Unavailable selection")) for value in sorted(missing)
        )
    return choices


def _label(obj: Any, key: str) -> str:
    text = str(obj)
    return f"#{getattr(obj, key)}" if text.startswith("<") else text


async def validate_relations(ctx: AdminContext, bound: BoundForm) -> None:
    for spec in bound.specs:
        if spec.type.name not in {"link", "multi_link"} or spec.name not in bound.cleaned:
            continue
        value = bound.cleaned[spec.name]
        values = list(dict.fromkeys(value)) if spec.type.name == "multi_link" else [value]
        values = [item for item in values if item is not None]
        rows = await related_objects(ctx, spec, pks=values) if values else []
        if len(rows) != len(values):
            bound.errors[spec.name] = ["Select a valid choice."]
        elif spec.type.name == "multi_link":
            bound.cleaned[spec.name] = values


async def save_relations(ctx: AdminContext, obj: Any, bound: BoundForm) -> None:
    for spec in bound.specs:
        if spec.type.name != "multi_link" or spec.name not in bound.cleaned:
            continue
        values = bound.cleaned[spec.name]
        rows = await related_objects(ctx, spec, pks=values) if values else []
        if len(rows) != len(values):
            raise PermissionDenied("A selected record is no longer available.")
        if spec.name in sa.inspect(obj).unloaded:
            await ctx.db.refresh(obj, [spec.name])
        setattr(obj, spec.name, rows)


async def build_cells(
    ctx: AdminContext, admin: ModelAdmin, specs: Sequence[FieldSpec], objects: Sequence[Any]
) -> list[list[Cell]]:
    allowed: dict[str, set[Any]] = {}
    related_display: dict[str, dict[Any, Cell]] = {}
    for spec in specs:
        if spec.options.get("display_relationship"):
            related_display[spec.name] = await _related_display_cells(ctx, admin, spec, objects)
            continue
        if spec.type.name not in {"link", "multi_link"} or spec.options.get("target") is None:
            continue
        pks: set[Any] = set()
        for obj in objects:
            if spec.type.name == "multi_link":
                pks.update(getattr(item, related_key(spec).key) for item in getattr(obj, spec.name))
            elif spec.column is not None:
                pks.add(getattr(obj, spec.column.key or spec.name, None))
        pks.discard(None)
        rows = await related_objects(ctx, spec, pks=list(pks)) if pks else []
        allowed[spec.name] = {getattr(row, related_key(spec).key) for row in rows}
    cells: list[list[Cell]] = []
    for obj in objects:
        row = []
        for spec in specs:
            if spec.secret:
                row.append(Cell())
            elif spec.options.get("display_relationship"):
                row.append(related_display[spec.name].get(admin.get_pk(obj), Cell()))
            elif spec.type.name == "multi_link":
                visible = [
                    item
                    for item in getattr(obj, spec.name)
                    if getattr(item, related_key(spec).key) in allowed.get(spec.name, set())
                ]
                row.append(spec.type.cell(visible, spec))
            elif (
                spec.name in allowed
                and spec.column is not None
                and getattr(obj, spec.column.key or spec.name, None) not in allowed[spec.name]
            ):
                row.append(Cell())
            else:
                row.append(admin.build_cell(spec, obj))
        cells.append(row)
    return cells


async def _related_display_cells(
    ctx: AdminContext,
    admin: ModelAdmin,
    spec: FieldSpec,
    objects: Sequence[Any],
) -> dict[Any, Cell]:
    """Load scalar related display values in one scoped query per column."""
    relationships = spec.options.get("relationship_path", ())
    if not relationships or any(relationship.property.uselist for relationship in relationships):
        return {}
    parent_pks = [admin.get_pk(obj) for obj in objects]
    if not parent_pks:
        return {}

    statement = sa.select(admin.pk_column()).select_from(admin.model)
    related_admins: list[tuple[ModelAdmin, AdminContext]] = []
    for relationship in relationships:
        model = relationship.property.mapper.class_
        related_admin = ctx.site.get_model_admin_for(model) or ModelAdmin(model, ctx.site)
        related_ctx = replace(ctx, admin=related_admin, obj=None)
        if not related_admin.has_view_permission(related_ctx):
            return {}
        primary_key = related_admin.pk_column()
        scope = (
            related_admin.get_queryset(related_ctx).with_only_columns(primary_key).order_by(None)
        )
        statement = statement.outerjoin(relationship.and_(primary_key.in_(scope)))
        statement = statement.add_columns(model)
        related_admins.append((related_admin, related_ctx))

    statement = statement.where(admin.pk_column().in_(parent_pks))
    rows = (await ctx.db.execute(statement)).unique().all()
    cells: dict[Any, Cell] = {}
    permission_cache: dict[tuple[type[Any], Any], bool] = {}
    for row in rows:
        parent_pk, related_objects = row[0], row[1:]
        visible = True
        for obj, (related_admin, related_ctx) in zip(related_objects, related_admins, strict=True):
            if obj is None:
                visible = False
                break
            key = (related_admin.model, related_admin.get_pk(obj))
            if key not in permission_cache:
                permission_cache[key] = related_admin.has_view_permission(
                    replace(related_ctx, obj=obj), obj
                )
            if not permission_cache[key]:
                visible = False
                break
        if not visible:
            continue
        target = related_objects[-1]
        value = getattr(target, spec.options["display_attribute"], None)
        cells[parent_pk] = spec.type.cell(value, spec)
    return cells
