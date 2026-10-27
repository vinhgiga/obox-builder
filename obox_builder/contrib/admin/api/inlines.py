"""Atomic partial child formsets scoped through their parent record."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict

from obox_builder.conf import settings
from obox_builder.contrib.admin.api.deps import DB, USER, json_body
from obox_builder.contrib.admin.api.etag import check_if_match
from obox_builder.contrib.admin.api.meta import model_context
from obox_builder.contrib.admin.api.problems import Problem
from obox_builder.contrib.admin.api.records import (
    batch_items,
    body_schema,
    get_record,
    merge_patch,
    representation,
    validate_keys,
)
from obox_builder.contrib.admin.api.serializers import (
    api_specs,
    field_value,
    record_values,
    serialize_records,
)
from obox_builder.contrib.admin.api.uploads import prepare_files
from obox_builder.contrib.admin.inlines import InlineRow
from obox_builder.contrib.admin.services import RecordService
from obox_builder.contrib.admin.uniqueness import validate_inline_uniqueness
from obox_builder.contrib.admin.uploads import persist_uploads
from obox_builder.core.exceptions import ImproperlyConfigured


class InlineInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = None
    fields: dict[str, Any] | None = None
    etag: str | None = None
    delete: bool = False


def inline_contexts(ctx: Any) -> list[Any]:
    result = []
    names = set()
    for index, inline in enumerate(ctx.admin.get_inline_instances(ctx)):
        if not inline.api_enabled:
            continue
        name = inline.api_name or inline.model_name
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ImproperlyConfigured("API inline names must be lowercase identifiers.")
        if name in names:
            raise ImproperlyConfigured("API inlines must have unique model names per parent.")
        names.add(name)
        child = replace(ctx, admin=inline, obj=None, extra={**ctx.extra, "parent": ctx.obj})
        if RecordService(child).allowed("view"):
            result.append((index, child))
    return result


def inline_schema(ctx: Any) -> list[dict[str, Any]]:
    return [
        {
            "name": child.admin.api_name or child.admin.model_name,
            "model": child.admin.model_label,
            "min_num": child.admin.min_num,
            "max_num": child.admin.get_max_num(),
            "can_add": RecordService(child).allowed("add"),
            "fields": [
                {
                    "name": spec.name,
                    "type": spec.type.name,
                    "readonly": not spec.is_editable(),
                    "nullable": spec.nullable,
                }
                for spec in api_specs(child)
            ],
        }
        for _index, child in inline_contexts(ctx)
    ]


async def inline_context(
    request: Request, user: Any, db: Any, label: str, pk: str, name: str
) -> Any:
    ctx = model_context(request, user, db, label)
    parent = await get_record(ctx, pk, lock=request.method == "PATCH")
    for index, child in inline_contexts(ctx):
        if (child.admin.api_name or child.admin.model_name) == name:
            return ctx, parent, index, child
    raise Problem(404, "not_found", "Inline is unavailable.")


def register_inlines(router: APIRouter) -> None:
    base = "/models/{label}/records/{pk}/inlines/{name}"

    @router.get(base)
    async def listing(
        request: Request,
        label: str,
        pk: str,
        name: str,
        user: USER,
        db: DB,
        page: int = Query(1, ge=1, le=100000),
        size: int = Query(50, ge=1, le=200),
    ) -> Any:
        ctx, parent, _index, child = await inline_context(request, user, db, label, pk, name)
        inline = child.admin
        rows = list(
            (
                await db.scalars(
                    inline.get_queryset(child)
                    .where(inline.fk_column == ctx.admin.get_pk(parent))
                    .order_by(inline.pk_column())
                    .offset((page - 1) * size)
                    .limit(size + 1)
                )
            ).unique()
        )
        objects = [
            obj
            for obj in rows[:size]
            if RecordService(replace(child, obj=obj)).allowed("view", obj)
        ]
        records, included = await serialize_records(child, objects)
        return {"records": records, "included": included, "has_next": len(rows) > size}

    @router.patch(base, openapi_extra=body_schema(InlineInput, batch_only=True))
    async def update(request: Request, label: str, pk: str, name: str, user: USER, db: DB) -> Any:
        ctx, parent, index, child = await inline_context(request, user, db, label, pk, name)
        parent_service = RecordService(ctx)
        parent_service.check_permission("change", parent)
        inline = child.admin
        rows = batch_items(await json_body(request))
        if len(rows) > min(settings.ADMIN_API_MAX_BATCH, inline.get_absolute_max()):
            raise Problem(422, "validation_error", "Too many inline rows.")
        formset = inline.get_formset(child, index)
        objects = list(
            (
                await db.scalars(
                    inline.get_queryset(child)
                    .where(inline.fk_column == ctx.admin.get_pk(parent))
                    .order_by(inline.pk_column())
                    .with_for_update(of=inline.model)
                    .execution_options(populate_existing=True)
                )
            ).unique()
        )
        visible = {
            str(inline.get_pk(obj)): obj
            for obj in objects
            if RecordService(replace(child, obj=obj)).allowed("view", obj)
        }
        all_rows = {
            key: InlineRow(obj=obj, values=record_values(obj, api_specs(replace(child, obj=obj))))
            for key, obj in visible.items()
        }
        changed = []
        deleted = []
        seen = set()
        uploads = []
        for data in rows:
            validate_keys(data, set(), {"id", "fields", "etag", "delete"})
            key = data.get("id")
            remove = data.get("delete", False)
            if data.get("etag") is not None and not isinstance(data["etag"], str):
                raise Problem(422, "validation_error", "Inline ETag must be a string.")
            if (key is not None and (not isinstance(key, str) or not key)) or not isinstance(
                remove, bool
            ):
                raise Problem(422, "validation_error", "Invalid inline row.")
            if key is not None and key in seen:
                raise Problem(422, "validation_error", "Duplicate inline ID.")
            if key is not None:
                seen.add(key)
            obj = visible.get(key) if key is not None else None
            if key is not None and obj is None:
                raise Problem(404, "not_found", "Inline record is unavailable.")
            row_ctx = replace(child, obj=obj)
            service = RecordService(row_ctx)
            if obj is not None:
                check_if_match(
                    (await representation(row_ctx, obj))["meta"]["etag"], data.get("etag")
                )
            row = InlineRow(obj=obj, values=data.get("fields", {}), delete=remove)
            if remove:
                if obj is None or "fields" in data:
                    raise Problem(422, "validation_error", "Deletion requires an ID and no fields.")
                service.check_permission("delete", obj)
                deleted.append(obj)
            else:
                validate_keys(data, {"fields"}, {"id", "etag", "delete"})
                values = data["fields"]
                specs = api_specs(row_ctx)
                if not isinstance(values, dict) or set(values) - {
                    s.name for s in specs if s.is_editable()
                }:
                    raise Problem(422, "validation_error", "Unknown or read-only inline field.")
                if any(
                    value is None and not spec.nullable
                    for spec in specs
                    for field, value in values.items()
                    if field == spec.name
                ):
                    raise Problem(422, "validation_error", "Inline field cannot be null.")
                values = dict(values)
                for spec in specs:
                    if (
                        obj is not None
                        and spec.type.name == "json"
                        and isinstance(values.get(spec.name), dict)
                    ):
                        values[spec.name] = merge_patch(field_value(obj, spec), values[spec.name])
                values, staged = await prepare_files(row_ctx, values, specs)
                uploads.extend(staged)
                # The parent FK is assigned by InlineFormSet; uniqueness checks need its value.
                row.bound = await service.bind(
                    values, target=obj, specs=specs, partial=obj is not None, api=True
                )
                if set(data["fields"]) - row.bound.cleaned.keys():
                    raise Problem(
                        422,
                        "validation_error",
                        "Inline field cannot be edited.",
                        errors=row.bound.errors,
                    )
                changed.append(row)
            if key is None:
                formset.rows.append(row)
            else:
                all_rows[key] = row
        formset.rows = [*all_rows.values(), *formset.rows]
        total = int(
            await db.scalar(
                sa.select(sa.func.count())
                .select_from(inline.model)
                .where(inline.fk_column == ctx.admin.get_pk(parent))
            )
            or 0
        )
        final_count = total + sum(row.obj is None for row in changed) - len(deleted)
        if not inline.min_num <= final_count <= inline.get_max_num():
            raise Problem(422, "validation_error", "Inline row count is outside configured limits.")
        await validate_inline_uniqueness(child, inline, formset.rows, parent)
        await formset.clean()
        if not formset.is_valid:
            raise Problem(
                422,
                "validation_error",
                "Invalid inline formset.",
                errors={
                    "__all__": formset.errors,
                    **{
                        str(i): row.bound.errors
                        for i, row in enumerate(formset.rows)
                        if row.bound and row.bound.errors
                    },
                },
            )
        # Enforce the API's cascade confirmation contract before InlineFormSet deletes.
        if deleted:
            from obox_builder.contrib.admin.api.records import plan_data
            from obox_builder.contrib.admin.deletion import confirmed

            plan = await RecordService(child).plan_delete(deleted, lock=True)
            if not plan.allowed:
                raise Problem(409, "conflict", "Related records prevent inline deletion.")
            if any(effect.depth > 0 for effect in plan.effects.values()) and not confirmed(
                child, plan, request.headers.get("x-delete-confirmation")
            ):
                raise Problem(
                    409,
                    "confirmation_required",
                    "Confirm related deletions.",
                    plan=plan_data(child, plan),
                )
        bound = await parent_service.bind({}, target=parent, partial=True, api=True)
        persist_uploads(uploads)
        request.state.api_uploads.extend((path, b"") for path, _content in uploads)
        await parent_service.save(bound, target=parent, formsets=[formset])
        result = []
        for row in changed:
            # New objects are retained by the formset after save.
            obj = row.obj
            if obj is not None:
                await db.refresh(obj)
                result.append(await representation(replace(child, obj=obj), obj))
        return {"records": result, "deleted": [str(inline.get_pk(obj)) for obj in deleted]}
