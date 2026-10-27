"""Related child forms validated and saved with their parent."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, ClassVar

import sqlalchemy as sa

from obox_builder.contrib.admin.context import AdminContext
from obox_builder.contrib.admin.options import ModelAdmin
from obox_builder.contrib.admin.relations import build_cells, relation_choices, validate_relations
from obox_builder.core.exceptions import ImproperlyConfigured, PermissionDenied
from obox_builder.fields import FieldSpec
from obox_builder.forms import BoundForm, render_values


class InlineModelAdmin(ModelAdmin):
    api_name: ClassVar[str | None] = None
    fk_name: ClassVar[str | None] = None
    extra: ClassVar[int] = 3
    min_num: ClassVar[int] = 0
    max_num: ClassVar[int | None] = None
    can_delete: ClassVar[bool] = True
    show_change_link: ClassVar[bool] = False
    formset_class: ClassVar[type[InlineFormSet] | None] = None
    layout: ClassVar[str] = "tabular"

    def __init__(self, parent_model: type[Any], site: Any = None) -> None:
        model = getattr(type(self), "model", None)
        if model is None:
            raise ImproperlyConfigured("An inline must declare its model.")
        super().__init__(model, site)
        self.parent_model = parent_model
        candidates = [
            column
            for column in sa.inspect(model).columns
            if any(
                fk.column is sa.inspect(parent_model).primary_key[0] for fk in column.foreign_keys
            )
        ]
        if self.fk_name:
            candidates = [column for column in candidates if column.key == self.fk_name]
        if len(candidates) != 1:
            raise ImproperlyConfigured("Set fk_name to one foreign key to the parent primary key.")
        self.fk_column = candidates[0]
        max_num = self.get_max_num()
        if self.min_num < 0 or self.extra < 0 or max_num < 0:
            raise ImproperlyConfigured("Inline limits must be non-negative.")

    def get_max_num(self) -> int:
        unique_foreign_key = self.fk_column.unique or any(
            isinstance(constraint, sa.UniqueConstraint)
            and tuple(constraint.columns) == (self.fk_column,)
            for constraint in self.fk_column.table.constraints
        )
        unique_foreign_key |= any(
            index.unique and tuple(index.columns) == (self.fk_column,)
            for index in self.fk_column.table.indexes
        )
        if unique_foreign_key:
            return 1
        return self.max_num if self.max_num is not None else 1000

    def get_absolute_max(self) -> int:
        """Cap forms parsed from a forged POST, as Django formsets do."""
        return self.get_max_num() + 1000

    def get_form_fields(self, ctx: AdminContext) -> list[FieldSpec]:
        return [spec for spec in super().get_form_fields(ctx) if spec.name != self.fk_column.key]

    def get_formset(self, ctx: AdminContext, index: int) -> InlineFormSet:
        """Build the inline formset; override or set ``formset_class`` to customize it."""
        formset_class = self.formset_class or InlineFormSet
        if not isinstance(formset_class, type) or not issubclass(formset_class, InlineFormSet):
            raise ImproperlyConfigured("formset_class must be an InlineFormSet subclass.")
        return formset_class(ctx, self, index)

    def get_queryset(self, ctx: AdminContext) -> sa.Select[Any]:
        registered = ctx.site.get_model_admin_for(self.model)
        if registered is not None:
            return registered.get_queryset(replace(ctx, admin=registered))
        return super().get_queryset(ctx)

    def _allowed(self, ctx: AdminContext, action: str, obj: Any = None) -> bool:
        checker = getattr(super(), f"has_{action}_permission")
        if not (checker(ctx) if action == "add" else checker(ctx, obj)):
            return False
        registered = ctx.site.get_model_admin_for(self.model)
        if registered is None:
            return True
        registered_ctx = replace(ctx, admin=registered)
        checker = getattr(registered, f"has_{action}_permission")
        return bool(checker(registered_ctx) if action == "add" else checker(registered_ctx, obj))

    def has_view_permission(self, ctx: AdminContext, obj: Any = None) -> bool:
        return self._allowed(ctx, "view", obj)

    def has_add_permission(self, ctx: AdminContext) -> bool:
        return self._allowed(ctx, "add")

    def has_change_permission(self, ctx: AdminContext, obj: Any = None) -> bool:
        return self._allowed(ctx, "change", obj)

    def has_delete_permission(self, ctx: AdminContext, obj: Any = None) -> bool:
        return self.can_delete and self._allowed(ctx, "delete", obj)


class TabularInline(InlineModelAdmin):
    layout = "tabular"


class StackedInline(InlineModelAdmin):
    layout = "stacked"


@dataclass
class InlineRow:
    obj: Any = None
    values: dict[str, Any] = field(default_factory=dict)
    bound: BoundForm | None = None
    delete: bool = False


@dataclass
class InlineFormSet:
    ctx: AdminContext
    inline: InlineModelAdmin
    index: int
    rows: list[InlineRow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def prefix(self) -> str:
        return f"inline:{self.index}:"

    @property
    def is_valid(self) -> bool:
        return not self.errors and all(row.bound is None or row.bound.is_valid for row in self.rows)

    async def clean(self) -> None:
        """Validate constraints shared by multiple child rows after binding."""

    async def save(self, parent: Any) -> None:
        from obox_builder.contrib.admin.deletion import collect_deletion

        for row in self.rows:
            if row.delete and row.obj is not None:
                child_ctx = replace(self.ctx, obj=row.obj)
                plan = await collect_deletion(child_ctx, [row.obj], lock=True)
                if not plan.allowed:
                    raise PermissionDenied("An inline record cannot be deleted safely.")
                await self.inline.delete_model(child_ctx, row.obj)
        await self.ctx.db.flush()
        for row in self.rows:
            if row.delete or row.bound is None:
                continue
            obj = row.obj if row.obj is not None else self.inline.model()
            if (
                row.obj is not None
                and all(
                    getattr(obj, name, None) == value
                    for name, value in row.bound.cleaned.items()
                    if name
                    in {spec.name for spec in row.bound.specs if spec.type.name != "multi_link"}
                )
                and not any(spec.type.name == "multi_link" for spec in row.bound.specs)
            ):
                continue
            ctx = replace(self.ctx, obj=obj, extra={**self.ctx.extra, "parent": parent})
            row.bound.apply_to(obj)
            setattr(
                obj,
                self.inline.fk_column.key,
                getattr(parent, sa.inspect(type(parent)).primary_key[0].key),
            )
            await self.inline.save_model(ctx, obj, row.bound, change=row.obj is not None)
            await self.inline.save_related(ctx, obj, row.bound, change=row.obj is not None)
            row.obj = obj
        await self.ctx.db.flush()

    def initial(self, ctx: AdminContext) -> dict[str, Any]:
        values = {}
        for spec in self.inline.get_form_fields(ctx):
            value = spec.initial(ctx.record_context()) if callable(spec.initial) else spec.initial
            if value is None and spec.column is not None:
                default = getattr(spec.column, "default", None)
                if default is not None and default.is_scalar:
                    value = default.arg
            values[spec.name] = (
                value or [] if spec.type.name == "multi_link" else spec.type.to_input(value, spec)
            )
        return values

    async def presentation(
        self, parent_admin: ModelAdmin, parent: Any, readonly: bool
    ) -> dict[str, Any]:
        rows = [
            await self._row_context(row, index, parent_admin, parent, readonly)
            for index, row in enumerate(self.rows)
        ]
        empty = await self._row_context(InlineRow(), "__index__", parent_admin, parent, readonly)
        return {
            "inline": self.inline,
            "rows": rows,
            "empty": empty,
            "prefix": self.prefix,
            "errors": self.errors,
            "can_add": not readonly and self.inline.has_add_permission(self.ctx),
            "max_num": self.inline.get_max_num(),
        }

    async def _row_context(
        self, row: InlineRow, index: Any, parent_admin: ModelAdmin, parent: Any, readonly: bool
    ) -> dict[str, Any]:
        ctx = replace(self.ctx, obj=row.obj)
        if row.obj is not None and sa.inspect(row.obj).expired_attributes:
            await ctx.db.refresh(row.obj)
        specs = [
            spec
            for spec in self.inline.get_form_fields(ctx)
            if not spec.secret and (row.obj is not None or not spec.readonly)
        ]
        if row.obj is not None:
            unloaded = sa.inspect(row.obj).unloaded
            relationships = [
                str(spec.options["relationship"])
                for spec in specs
                if spec.options.get("relationship") in unloaded
            ]
            if relationships:
                await ctx.db.refresh(row.obj, relationships)
        values = row.values or (
            render_values(row.obj, specs) if row.obj is not None else self.initial(ctx)
        )
        can_change = not readonly and (
            self.inline.has_add_permission(ctx)
            if row.obj is None
            else self.inline.has_change_permission(ctx, row.obj)
        )
        cells = (
            (await build_cells(ctx, self.inline, specs, [row.obj]))[0]
            if row.obj is not None
            else []
        )
        urls, choices = {}, {}
        for spec in specs:
            if spec.type.name == "single_select":
                getter = getattr(spec.type, "choices", None)
                choices[spec.name] = (
                    [(str(v), str(label)) for v, label in getter(spec)] if getter else []
                )
            elif spec.type.name in {"link", "multi_link"}:
                autocomplete = spec.name in self.inline.get_autocomplete_fields(ctx)
                choices[spec.name] = await relation_choices(
                    ctx, spec, values.get(spec.name), autocomplete=autocomplete
                )
                if autocomplete:
                    params = {"inline": str(self.index)}
                    if parent is not None:
                        params["parent"] = str(parent_admin.get_pk(parent))
                    if row.obj is not None:
                        params["object"] = str(self.inline.get_pk(row.obj))
                    from urllib.parse import urlencode

                    urls[spec.name] = (
                        f"{parent_admin.list_path()}/autocomplete/{spec.name}?{urlencode(params)}"
                    )
        change_url = None
        change_label = None
        if row.obj is not None and self.inline.show_change_link:
            child_admin = ctx.site.get_model_admin_for(self.inline.model)
            if child_admin is not None:
                child_ctx = replace(ctx, admin=child_admin, obj=row.obj)
                can_view = child_admin.has_view_permission(child_ctx, row.obj)
                can_change_child = child_admin.has_change_permission(child_ctx, row.obj)
                if can_view or can_change_child:
                    change_url = child_admin.change_path(self.inline.get_pk(row.obj))
                    change_label = "Edit details" if can_change_child else "View details"
        return {
            "obj": row.obj,
            "pk": self.inline.get_pk(row.obj) if row.obj is not None else "",
            "prefix": f"{self.prefix}{index}:",
            "specs": specs,
            "values": values,
            "errors": row.bound.errors if row.bound else {},
            "delete": row.delete,
            "readonly": not can_change,
            "can_delete": not readonly
            and row.obj is not None
            and self.inline.has_delete_permission(ctx, row.obj),
            "change_url": change_url,
            "change_label": change_label,
            "choices": choices,
            "cells": dict(zip((spec.name for spec in specs), cells, strict=row.obj is not None)),
            "autocomplete_urls": urls,
        }


async def load_inlines(
    ctx: AdminContext, parent: Any, data: dict[str, Any] | None = None
) -> list[InlineFormSet]:
    from obox_builder.contrib.admin.deletion import collect_deletion
    from obox_builder.contrib.admin.uniqueness import validate_inline_uniqueness

    if ctx.admin is None:
        return []
    formsets = []
    for index, inline in enumerate(ctx.admin.get_inline_instances(ctx)):
        inline_ctx = replace(ctx, admin=inline, obj=None, extra={**ctx.extra, "parent": parent})
        formset = inline.get_formset(inline_ctx, index)
        can_add = inline.has_add_permission(inline_ctx)
        can_view = inline.has_view_permission(inline_ctx) or inline.has_change_permission(
            inline_ctx
        )
        if not can_view and not can_add:
            if data and any(key.startswith(formset.prefix) for key in data):
                raise PermissionDenied("This inline is unavailable.")
            continue
        objects = []
        total = 0
        if parent is not None:
            pk = ctx.admin.get_pk(parent)
            if can_view:
                objects = list(
                    (
                        await ctx.db.scalars(
                            inline.get_queryset(inline_ctx)
                            .where(inline.fk_column == pk)
                            .order_by(inline.pk_column())
                        )
                    ).unique()
                )
                objects = [
                    obj
                    for obj in objects
                    if inline.has_view_permission(replace(inline_ctx, obj=obj), obj)
                    or inline.has_change_permission(replace(inline_ctx, obj=obj), obj)
                ]
            total = int(
                await ctx.db.scalar(
                    sa.select(sa.func.count())
                    .select_from(inline.model)
                    .where(inline.fk_column == pk)
                )
                or 0
            )
        if data is None:
            formset.rows = [InlineRow(obj=obj) for obj in objects]
            if can_add:
                max_num = inline.get_max_num()
                initial_forms = max(total, inline.min_num)
                total_forms = initial_forms + inline.extra
                total_forms = total if total > max_num else min(total_forms, max_num)
                formset.rows.extend(InlineRow() for _ in range(max(0, total_forms - total)))
        else:
            try:
                count = int(data.get(f"{formset.prefix}_total", "0"))
            except (TypeError, ValueError):
                count = -1
            if not 0 <= count <= inline.get_absolute_max():
                formset.errors.append("Invalid number of inline forms.")
                formsets.append(formset)
                continue
            known = {str(inline.get_pk(obj)): obj for obj in objects}
            seen: set[str] = set()
            for position in range(count):
                prefix = f"{formset.prefix}{position}:"
                raw = {
                    key[len(prefix) :]: value
                    for key, value in data.items()
                    if key.startswith(prefix)
                }
                identity = str(raw.get("_pk", ""))
                obj = known.get(identity)
                if identity and (obj is None or identity in seen):
                    raise PermissionDenied("An inline record is unavailable.")
                seen.add(identity) if identity else None
                child_ctx = replace(inline_ctx, obj=obj)
                specs = inline.get_form_fields(child_ctx)
                row = InlineRow(obj=obj, values=raw, delete=raw.get("_delete") == "1")
                formset.rows.append(row)
                if row.delete:
                    if obj is not None:
                        inline.check_permission(child_ctx, "delete", obj)
                        plan = await collect_deletion(child_ctx, [obj])
                        if not plan.allowed:
                            formset.errors.append("A related record prevents this inline deletion.")
                    continue
                initial = formset.initial(child_ctx)
                editable = [spec for spec in specs if spec.is_editable()]
                if obj is None and all(
                    spec.type.from_input(raw.get(spec.name), spec)
                    == spec.type.from_input(initial.get(spec.name), spec)
                    for spec in editable
                ):
                    continue
                if obj is None:
                    inline.check_permission(child_ctx, "add")
                elif not inline.has_change_permission(child_ctx, obj):
                    if any(spec.name in raw for spec in editable):
                        raise PermissionDenied("You cannot change this inline record.")
                    continue
                row.bound = BoundForm.bind(
                    specs,
                    inline.get_form(child_ctx, change=obj is not None),
                    raw,
                    base=child_ctx.record_context(),
                )
                if row.bound.is_valid:
                    await validate_relations(child_ctx, row.bound)
            await validate_inline_uniqueness(ctx, inline, formset.rows, parent)
            await formset.clean()
        formsets.append(formset)
    return formsets
