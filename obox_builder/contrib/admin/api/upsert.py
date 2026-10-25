"""Conditional upsert through declared, uniquely constrained import keys."""

from __future__ import annotations

import json
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, Request
from sqlalchemy.exc import IntegrityError

from obox_builder.contrib.admin.api.deps import DB, USER, json_body
from obox_builder.contrib.admin.api.etag import check_if_match
from obox_builder.contrib.admin.api.meta import model_context
from obox_builder.contrib.admin.api.problems import Problem
from obox_builder.contrib.admin.api.records import (
    RecordInput,
    body_schema,
    representation,
    save_record,
    validate_keys,
)
from obox_builder.contrib.admin.api.serializers import api_specs
from obox_builder.contrib.admin.services import RecordService
from obox_builder.contrib.imports.resources import ImportResource
from obox_builder.core.exceptions import ImproperlyConfigured
from obox_builder.forms import BoundForm


class UpsertRecord(RecordInput):
    etag: str = ""


def upsert_body() -> dict[str, Any]:
    extra = body_schema(UpsertRecord, batch_only=True)
    schema = extra["requestBody"]["content"]["application/json"]["schema"]
    schema["required"].append("merge_on")
    schema["properties"]["merge_on"] = {
        "type": "array",
        "items": {"type": "string"},
        "minItems": 1,
        "uniqueItems": True,
    }
    return extra


def register_upsert(router: APIRouter) -> None:
    @router.patch("/models/{label}/records:upsert", openapi_extra=upsert_body())
    async def upsert(request: Request, label: str, user: USER, db: DB) -> Any:
        from obox_builder.conf import settings

        ctx = model_context(request, user, db, label)
        service = RecordService(ctx)
        admin = service.admin
        body = await json_body(request)
        validate_keys(body, {"merge_on", "records"})
        keys, records = body["merge_on"], body["records"]
        if (
            not isinstance(keys, list)
            or not keys
            or not all(isinstance(key, str) for key in keys)
            or len(set(keys)) != len(keys)
            or not set(keys) <= set(admin.import_keys)
        ):
            raise Problem(422, "validation_error", "merge_on must use declared import keys.")
        if not isinstance(records, list) or not 1 <= len(records) <= settings.ADMIN_API_MAX_BATCH:
            raise Problem(422, "validation_error", "Invalid upsert batch size.")
        resource = ImportResource(admin.model, user, keys=keys, specs=api_specs(ctx))
        try:
            resource.check_configuration("upsert")
        except ImproperlyConfigured as exc:
            raise Problem(422, "validation_error", str(exc)) from exc
        prepared = []
        signatures = set()
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                raise Problem(422, "validation_error", "Each record must be an object.")
            validate_keys(record, {"fields"}, {"etag"})
            fields = record["fields"]
            if not isinstance(fields, dict) or not isinstance(record.get("etag", ""), str):
                raise Problem(422, "validation_error", "Invalid upsert record.")
            bound = BoundForm.bind(
                resource.key_specs,
                resource.key_schema,
                {key: fields[key] for key in keys if key in fields},
                api=True,
            )
            if not bound.is_valid:
                raise Problem(
                    422, "validation_error", "Invalid merge key.", errors={str(index): bound.errors}
                )
            signature = json.dumps(bound.cleaned, sort_keys=True, default=str)
            if signature in signatures:
                raise Problem(422, "validation_error", "Duplicate merge key in batch.")
            signatures.add(signature)
            prepared.append((signature, index, record, bound.cleaned))
        output: dict[int, Any] = {}
        async with db.begin_nested():
            for _signature, index, record, values in sorted(prepared):
                predicates = [resource.columns[key].column == values[key] for key in keys]
                stmt = (
                    admin.get_queryset(ctx)
                    .where(*predicates)
                    .with_for_update(of=admin.model)
                    .execution_options(populate_existing=True)
                )

                target = (await db.scalars(stmt)).unique().one_or_none()
                if target is None:
                    existing = await db.scalar(sa.select(admin.pk_column()).where(*predicates))
                    if existing is not None:
                        target = (await db.scalars(stmt)).unique().one_or_none()
                        if target is None:
                            raise Problem(404, "not_found", "Record is unavailable.")
                ctx.obj = target
                fields = dict(record["fields"])
                immutable = {spec.name for spec in api_specs(ctx) if not spec.is_editable()}
                if target is None and immutable & set(keys):
                    raise Problem(
                        422,
                        "validation_error",
                        "Read-only keys can only identify existing records.",
                    )
                if target is None:
                    service.check_permission("add")
                    try:
                        target = await save_record(ctx, fields)
                        output[index] = {**await representation(ctx, target), "created": True}
                        continue
                    except (IntegrityError, Problem) as exc:
                        if isinstance(exc, Problem) and exc.status != 422:
                            raise
                        target = (await db.scalars(stmt)).unique().one_or_none()
                        if target is None:
                            raise
                ctx.obj = target
                if not admin.has_view_permission(ctx, target):
                    raise Problem(404, "not_found", "Record is unavailable.")
                service.check_permission("change", target)
                current = await representation(ctx, target)
                check_if_match(current["meta"]["etag"], record.get("etag"))
                for key in immutable & set(keys):
                    fields.pop(key, None)
                target = await save_record(ctx, fields, target)
                output[index] = {**await representation(ctx, target), "created": False}
        return {"records": [output[index] for index in range(len(records))]}
