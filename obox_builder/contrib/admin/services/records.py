"""HTML form binding and presentation around guarded resource operations."""

from __future__ import annotations

import builtins
from collections.abc import Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel

from obox_builder.conf import settings
from obox_builder.contrib.admin.context import AdminContext
from obox_builder.contrib.admin.pagination import AdminPage
from obox_builder.contrib.admin.relations import related_key, validate_relations
from obox_builder.contrib.admin.services.deletion import DeletionPlan, collect_deletion
from obox_builder.contrib.uploads import discard_uploads, persist_uploads
from obox_builder.exceptions import PermissionDenied
from obox_builder.fields import FieldSpec, QuerySpec
from obox_builder.forms import BoundForm
from obox_builder.resources import ModelResource


def field_value(obj: Any, spec: FieldSpec) -> Any:
    attr = str(spec.options.get("value_attr") or spec.name)
    if spec.type.name == "link" and spec.column is not None:
        attr = str(spec.column.key)
    return getattr(obj, attr, None)


def record_values(obj: Any, specs: Sequence[FieldSpec]) -> dict[str, Any]:
    values = {}
    for spec in specs:
        value = field_value(obj, spec)
        if spec.type.name == "multi_link":
            key = related_key(spec)
            value = [getattr(item, key.key) for item in value or []]
        values[spec.name] = value
    return values


class RecordService:
    def __init__(self, ctx: AdminContext) -> None:
        if ctx.admin is None:
            raise ValueError("RecordService requires a model admin.")
        self.ctx = ctx
        self.admin = ctx.admin

    def allowed(self, action: str, obj: Any = None) -> bool:
        key = {"view": "get", "add": "create", "change": "update"}.get(action, action)
        checker = getattr(self.admin, f"has_{action}_permission")
        return self.admin.resource.can_execute(
            self.admin.operation_context(self.ctx), key, obj
        ) and bool(checker(self.ctx) if action == "add" else checker(self.ctx, obj))

    def check_permission(self, action: str, obj: Any = None) -> None:
        if not self.allowed(action, obj):
            raise PermissionDenied(f"You do not have permission to {action} this record.")

    async def get(self, pk: str, *, for_update: bool = False) -> Any:
        return await self.admin.get_object(self.ctx, pk, for_update=for_update)

    async def list(
        self, query: QuerySpec, *, queryset: Any = None, total: str | None = None
    ) -> AdminPage:
        ctx, admin = self.ctx, self.admin
        self.check_permission("view")
        stmt = (
            queryset
            if queryset is not None
            else query.apply(
                admin.get_queryset(ctx),
                admin.get_query_specs(ctx),
                search_specs=admin.get_search_specs(ctx),
                text_collation=settings.ADMIN_TEXT_COLLATION,
            )
        )
        if total is None:
            return await admin.get_paginator(ctx, stmt).get_page(query.page, query.size)
        stmt = stmt.order_by(admin.pk_column())
        if ctx.db.get_bind().dialect.name == "postgresql":
            await ctx.db.execute(
                sa.text("SELECT set_config('statement_timeout', :timeout, true)"),
                {"timeout": str(settings.API_QUERY_TIMEOUT_MS)},
            )
        count = None
        if total != "none":
            count_stmt = stmt.with_only_columns(admin.pk_column()).order_by(None).distinct()
            if total == "capped":
                count_stmt = count_stmt.limit(10_001)
            count = int(
                await ctx.db.scalar(sa.select(sa.func.count()).select_from(count_stmt.subquery()))
                or 0
            )
        rows = (
            (await ctx.db.scalars(stmt.offset((query.page - 1) * query.size).limit(query.size + 1)))
            .unique()
            .all()
        )
        return AdminPage(
            rows[: query.size],
            query.page,
            query.size,
            count,
            None,
            len(rows) > query.size,
            query.page > 1,
        )

    async def bind(
        self,
        data: Mapping[str, Any],
        *,
        target: Any = None,
        specs: Sequence[FieldSpec] | None = None,
        schema: type[BaseModel] | None = None,
        errors: Mapping[str, builtins.list[str]] | None = None,
        partial: bool = False,
        api: bool = False,
    ) -> BoundForm:
        ctx, admin = self.ctx, self.admin
        ctx.obj = target
        self.check_permission("add" if target is None else "change", target)
        bound = BoundForm.bind(
            specs if specs is not None else admin.get_form_fields(ctx),
            schema if schema is not None else admin.get_form(ctx, change=target is not None),
            data,
            base=ctx.record_context(),
            partial=partial,
            api=api,
        )
        bound.errors.update(errors or {})
        if bound.is_valid:
            await validate_relations(ctx, bound)
        if bound.is_valid:
            from obox_builder.resources.errors import ResourceValidationError

            if not isinstance(admin.resource, ModelResource):
                raise PermissionDenied("This resource is read-only.")
            try:
                await admin.resource.bind(admin.operation_context(ctx), self.payload(bound), target)
            except ResourceValidationError as exc:
                bound.errors.update(exc.errors)
                bound.parsed = None
        return bound

    async def save(
        self,
        bound: BoundForm,
        *,
        target: Any = None,
        formsets: Sequence[Any] = (),
        uploads: Sequence[Any] = (),
    ) -> Any:
        if not bound.is_valid or not all(formset.is_valid for formset in formsets):
            raise ValueError("Cannot save an invalid form.")
        ctx, admin = self.ctx, self.admin
        self.check_permission("add" if target is None else "change", target)
        resource = admin.resource
        operation = "create" if target is None else "update"
        payload = self.payload(bound)
        try:
            persist_uploads(uploads)
            async with ctx.db.begin_nested():
                result = await resource.execute(
                    operation, admin.operation_context(ctx), payload, target=target
                )
                identity = getattr(result, resource.pk_column().key)
                obj = await ctx.db.get(admin.model, resource.parse_pk(identity))
                ctx.obj = obj
                for formset in formsets:
                    await formset.save(obj)
                await ctx.db.flush()
        except BaseException:
            discard_uploads(uploads)
            raise
        return obj

    def payload(self, bound: BoundForm) -> dict[str, Any]:
        specs = self.admin.resource.specs
        return {
            name: specs[name].type.to_api(value, specs[name])
            for name, value in bound.cleaned.items()
            if name in specs and specs[name].is_editable()
        }

    async def plan_delete(self, targets: builtins.list[Any], *, lock: bool = False) -> DeletionPlan:
        return await collect_deletion(self.ctx, targets, lock=lock)

    async def delete(self, target: Any) -> None:
        self.ctx.obj = target
        self.check_permission("delete", target)
        await self.admin.resource.execute(
            "delete", self.admin.operation_context(self.ctx), target=target
        )
