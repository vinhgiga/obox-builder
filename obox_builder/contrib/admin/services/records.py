"""Transport-independent record validation and writes for admin hooks."""

from __future__ import annotations

import builtins
from collections.abc import Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel

from obox_builder.conf import settings
from obox_builder.contrib.admin.context import AdminContext
from obox_builder.contrib.admin.deletion import DeletionPlan, collect_deletion
from obox_builder.contrib.admin.pagination import AdminPage
from obox_builder.contrib.admin.relations import validate_relations
from obox_builder.contrib.admin.uniqueness import validate_model_uniqueness
from obox_builder.contrib.admin.uploads import discard_uploads, persist_uploads
from obox_builder.fields import FieldSpec, QuerySpec
from obox_builder.forms import BoundForm


class RecordService:
    def __init__(self, ctx: AdminContext) -> None:
        if ctx.admin is None:
            raise ValueError("RecordService requires a model admin.")
        self.ctx = ctx
        self.admin = ctx.admin

    async def get(self, pk: str, *, for_update: bool = False) -> Any:
        return await self.admin.get_object(self.ctx, pk, for_update=for_update)

    async def list(
        self, query: QuerySpec, *, queryset: Any = None, total: str | None = None
    ) -> AdminPage:
        ctx, admin = self.ctx, self.admin
        admin.check_permission(ctx, "view")
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
                {"timeout": str(settings.ADMIN_API_QUERY_TIMEOUT_MS)},
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
        admin.check_permission(ctx, "add" if target is None else "change", target)
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
            await validate_model_uniqueness(ctx, admin.model, bound, target)
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
        admin.check_permission(ctx, "add" if target is None else "change", target)
        obj = target if target is not None else admin.model()
        ctx.obj = obj
        try:
            persist_uploads(uploads)
            async with ctx.db.begin_nested():
                bound.apply_to(obj)
                await admin.save_model(ctx, obj, bound, change=target is not None)
                await admin.save_related(ctx, obj, bound, change=target is not None)
                for formset in formsets:
                    await formset.save(obj)
                await ctx.db.flush()
        except BaseException:
            discard_uploads(uploads)
            raise
        return obj

    async def plan_delete(self, targets: builtins.list[Any], *, lock: bool = False) -> DeletionPlan:
        return await collect_deletion(self.ctx, targets, lock=lock)

    async def delete(self, target: Any) -> None:
        self.ctx.obj = target
        self.admin.check_permission(self.ctx, "delete", target)
        async with self.ctx.db.begin_nested():
            await self.admin.delete_model(self.ctx, target)
            await self.ctx.db.flush()
