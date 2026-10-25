"""Evaluate server-owned field rules against an unsaved draft."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from obox_builder.contrib.admin.api.deps import DB, USER, json_body
from obox_builder.contrib.admin.api.meta import model_context
from obox_builder.contrib.admin.api.problems import Problem
from obox_builder.contrib.admin.api.records import (
    RecordInput,
    body_schema,
    get_record,
    validate_keys,
)
from obox_builder.contrib.admin.api.serializers import api_specs, record_values
from obox_builder.contrib.admin.services import RecordService


class FormDraft(RecordInput):
    id: str = ""


def register_forms(router: APIRouter) -> None:
    @router.post("/models/{label}/records/_form-state", openapi_extra=body_schema(FormDraft))
    async def state(request: Request, label: str, user: USER, db: DB) -> Any:
        ctx = model_context(request, user, db, label)
        body = await json_body(request)
        validate_keys(body, {"fields"}, {"id"})
        if not isinstance(body["fields"], dict) or (
            "id" in body and not isinstance(body["id"], str)
        ):
            raise Problem(422, "validation_error", "Invalid form draft.")
        target = await get_record(ctx, body["id"]) if "id" in body else None
        ctx.obj = target
        specs = api_specs(ctx)
        fields = body["fields"]
        writable = {spec.name for spec in specs if spec.is_editable()}
        if fields.keys() - writable:
            raise Problem(422, "validation_error", "Unknown or read-only draft field.")
        service = RecordService(ctx)
        from obox_builder.contrib.admin.api.uploads import prepare_files

        draft, _uploads = await prepare_files(ctx, fields, specs)
        bound = await service.bind(
            draft, target=target, specs=specs, partial=target is not None, api=True
        )
        for spec in specs:
            if spec.name in fields and fields[spec.name] is None and not spec.nullable:
                bound.errors.setdefault(spec.name, ["This field cannot be null."])
        values = record_values(target, specs) if target is not None else {}
        values.update(fields)
        values.update(bound.cleaned)
        rule_ctx = ctx.record_context(values)
        states = {}
        for spec in specs:
            visible = spec.show_if is None or bool(spec.show_if(rule_ctx))
            editable = spec.is_editable() and (
                spec.editable_if is None or bool(spec.editable_if(rule_ctx))
            )
            required = spec.required or (
                spec.required_if is not None and bool(spec.required_if(rule_ctx))
            )
            errors = list(bound.errors.get(spec.name, []))
            if spec.valid_if is not None and spec.name not in fields and not errors:
                message = spec.valid_if(values.get(spec.name), rule_ctx)
                if message:
                    errors.append(str(message))
                    bound.errors[spec.name] = errors
            item: dict[str, Any] = {
                "visible": visible,
                "editable": editable,
                "required": required,
                "errors": errors,
            }
            if target is None and spec.name not in fields and spec.initial is not None and visible:
                value = spec.initial(rule_ctx) if callable(spec.initial) else spec.initial
                item["initial"] = spec.type.to_api(value, spec)
            states[spec.name] = item
        return {"fields": states, "errors": bound.errors, "valid": bound.is_valid}
