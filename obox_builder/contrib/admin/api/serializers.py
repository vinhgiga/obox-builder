"""Field allowlists and batched, permission-scoped JSON representations."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote

import sqlalchemy as sa

from obox_builder.contrib.admin.api.etag import etag
from obox_builder.contrib.admin.api.problems import Problem
from obox_builder.contrib.admin.context import AdminContext
from obox_builder.contrib.admin.relations import related_key, related_objects
from obox_builder.fields import FieldSpec


def api_specs(ctx: AdminContext) -> list[FieldSpec]:
    if ctx.admin is None:
        raise ValueError("A model admin is required.")
    mapper = sa.inspect(ctx.admin.model)
    protected = {"id", "created_at", "updated_at"}
    if mapper.version_id_col is not None:
        protected.add(mapper.get_property_by_column(mapper.version_id_col).key)
    return [
        replace(spec, readonly=True, editable=False)
        if spec.primary_key
        or spec.name in protected
        or spec.options.get("computed")
        or spec.type.name == "file"
        else spec
        for spec in ctx.admin.get_form_fields(ctx)
        if not spec.secret
    ]


def field_value(obj: Any, spec: FieldSpec) -> Any:
    attr = str(spec.options.get("value_attr") or spec.name)
    if spec.type.name == "link" and spec.column is not None:
        attr = str(spec.column.key)
    return getattr(obj, attr, None)


async def serialize_records(
    ctx: AdminContext, objects: Sequence[Any], selected: Sequence[str] | None = None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    admin = ctx.admin
    if admin is None:
        raise ValueError("A model admin is required.")
    row_specs = [api_specs(replace(ctx, obj=obj)) for obj in objects]
    specs = list({spec.name: spec for row in row_specs for spec in row}.values())
    if not objects:
        specs = api_specs(ctx)
    if selected is not None and set(selected) - {spec.name for spec in specs}:
        raise Problem(400, "invalid_query", "Unknown or unavailable field selection.")
    included: dict[str, Any] = {}
    visible: dict[str, dict[str, Any]] = {}
    for spec in specs:
        if spec.type.name not in {"link", "multi_link"} or "target" not in spec.options:
            continue
        key = related_key(spec)
        ids: set[Any] = set()
        for obj, allowed_specs in zip(objects, row_specs, strict=True):
            if spec not in allowed_specs or (
                spec.show_if is not None
                and not spec.show_if(replace(ctx, obj=obj).record_context())
            ):
                continue
            value = field_value(obj, spec)
            if spec.type.name == "multi_link":
                ids.update(getattr(item, key.key) for item in value or [])
            elif value is not None:
                ids.add(value)
        rows = await related_objects(ctx, spec, pks=list(ids)) if ids else []
        visible[spec.name] = {str(getattr(row, key.key)): row for row in rows}
        if selected is None or spec.name in selected:
            included[spec.name] = {
                pk: {"id": pk, "label": _label(row, getattr(row, key.key))}
                for pk, row in visible[spec.name].items()
            }
    result = []
    for obj, allowed_specs in zip(objects, row_specs, strict=True):
        row_ctx = replace(ctx, obj=obj)
        fields: dict[str, Any] = {}
        for spec in allowed_specs:
            if spec.show_if is not None and not spec.show_if(row_ctx.record_context()):
                continue
            value = field_value(obj, spec)
            if spec.type.name == "multi_link":
                key = related_key(spec)
                value = [
                    str(getattr(item, key.key))
                    for item in value or []
                    if str(getattr(item, key.key)) in visible.get(spec.name, {})
                ]
            elif spec.type.name == "link" and "target" in spec.options:
                value = str(value) if str(value) in visible.get(spec.name, {}) else None
            elif spec.type.name == "file":
                base = ctx.request.url.path.split("/models/")[0]
                record_id = quote(str(admin.get_pk(obj)), safe="")
                url = f"{base}/models/{admin.model_label}/records/{record_id}/files/{spec.name}"
                value = {"name": PurePosixPath(value).name, "url": url} if value else None
            elif spec.options.get("computed"):
                value = admin.build_cell(spec, obj).value
                value = spec.type.to_api(value, spec)
            else:
                value = spec.type.to_api(value, spec)
            fields[spec.name] = value
        mapper = sa.inspect(admin.model)
        version = (
            getattr(obj, mapper.get_property_by_column(mapper.version_id_col).key)
            if mapper.version_id_col is not None
            else None
        )
        label = _label(obj, admin.get_pk(obj))
        can_change = admin.has_change_permission(row_ctx, obj)
        can_delete = admin.has_delete_permission(row_ctx, obj)
        state = {
            "fields": fields,
            "label": label,
            "can_change": can_change,
            "can_delete": can_delete,
        }
        result.append(
            {
                "id": str(admin.get_pk(obj)),
                "label": label,
                "fields": fields
                if selected is None
                else {name: fields[name] for name in selected if name in fields},
                "meta": {
                    "etag": etag(state, version),
                    "can_change": can_change,
                    "can_delete": can_delete,
                },
            }
        )
    return result, included


def _label(obj: Any, pk: Any) -> str:
    text = str(obj)
    return f"#{pk}" if text.startswith("<") else text
