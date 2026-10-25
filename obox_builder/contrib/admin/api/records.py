"""Scoped model records; every write calls the shared record service."""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from obox_builder.conf import settings
from obox_builder.contrib.admin.api import idempotency
from obox_builder.contrib.admin.api.deps import DB, USER, json_body
from obox_builder.contrib.admin.api.etag import check_if_match
from obox_builder.contrib.admin.api.meta import model_context, register_meta
from obox_builder.contrib.admin.api.problems import Problem
from obox_builder.contrib.admin.api.queries import QueryRequest, list_records
from obox_builder.contrib.admin.api.serializers import api_specs, field_value, serialize_records
from obox_builder.contrib.admin.context import AdminContext
from obox_builder.contrib.admin.deletion import DeletionPlan, confirmation_token, confirmed
from obox_builder.contrib.admin.relations import related_admin, related_key, related_objects
from obox_builder.contrib.admin.services import RecordService
from obox_builder.contrib.admin.uploads import resolve_upload
from obox_builder.fields.types.choice import normalize_choices


class RecordInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fields: dict[str, Any]


class UpdateInput(RecordInput):
    id: str
    etag: str | None = None


class DeleteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[str] = Field(min_length=1)
    etags: dict[str, str] = Field(default_factory=dict)


class RecordOutput(BaseModel):
    id: str
    label: str
    fields: dict[str, Any]
    meta: dict[str, Any]


class RecordPage(BaseModel):
    records: list[RecordOutput]
    page: dict[str, Any]
    included: dict[str, Any]


def body_schema(
    schema: type[BaseModel], *, batch: bool = False, batch_only: bool = False
) -> dict[str, Any]:
    value = schema.model_json_schema()
    if batch or batch_only:
        batch_value = {
            "type": "object",
            "additionalProperties": False,
            "required": ["records"],
            "properties": {
                "records": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": settings.ADMIN_API_MAX_BATCH,
                    "items": value,
                }
            },
        }
        value = batch_value if batch_only else {"oneOf": [value, batch_value]}
    return {
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": value}},
        }
    }


def validate_keys(
    data: dict[str, Any], required: set[str], optional: set[str] | None = None
) -> None:
    if required - data.keys() or data.keys() - required - (optional or set()):
        raise Problem(422, "validation_error", "Missing or unknown request properties.")


async def get_record(ctx: AdminContext, pk: str, *, lock: bool = False) -> Any:
    obj = await RecordService(ctx).get(pk, for_update=lock)
    ctx.obj = obj
    if not RecordService(ctx).admin.has_view_permission(ctx, obj):
        raise Problem(404, "not_found", "Record is unavailable.")
    return obj


async def representation(ctx: AdminContext, obj: Any) -> dict[str, Any]:
    records, _included = await serialize_records(ctx, [obj])
    return records[0]


def merge_patch(target: Any, patch: Any) -> Any:
    if not isinstance(patch, dict):
        return patch
    result = dict(target) if isinstance(target, dict) else {}
    for name, value in patch.items():
        if value is None:
            result.pop(name, None)
        else:
            result[name] = merge_patch(result.get(name), value)
    return result


async def save_record(ctx: AdminContext, fields: Any, target: Any = None) -> Any:
    if not isinstance(fields, dict):
        raise Problem(422, "validation_error", "fields must be an object.")
    ctx.obj = target
    specs = api_specs(ctx)
    writable = {
        spec.name: spec for spec in specs if spec.is_editable() and spec.type.name != "file"
    }
    errors: dict[str, list[str]] = {}
    data = dict(fields)
    for name, value in data.items():
        spec = writable.get(name)
        if spec is None:
            errors[name] = ["Unknown or read-only field."]
        elif value is None and not spec.nullable:
            errors[name] = ["This field cannot be null."]
        elif target is not None and spec.type.name == "json" and isinstance(value, dict):
            data[name] = merge_patch(field_value(target, spec), value)
    if errors:
        raise Problem(422, "validation_error", "Invalid record fields.", errors=errors)
    service = RecordService(ctx)
    bound = await service.bind(
        data, target=target, specs=specs, partial=target is not None, api=True
    )
    if not bound.is_valid:
        raise Problem(422, "validation_error", "Invalid record fields.", errors=bound.errors)
    denied = {
        name: ["This field cannot be edited."] for name in fields if name not in bound.cleaned
    }
    if denied:
        raise Problem(422, "validation_error", "Invalid record fields.", errors=denied)
    obj = await service.save(bound, target=target)
    # Reload server-generated columns and relationship state before encoding JSON.
    await ctx.db.refresh(obj)
    return await service.get(str(RecordService(ctx).admin.get_pk(obj)), for_update=True)


def batch_items(body: dict[str, Any]) -> list[dict[str, Any]]:
    validate_keys(body, {"records"})
    records = body["records"]
    if (
        not isinstance(records, list)
        or not 1 <= len(records) <= settings.ADMIN_API_MAX_BATCH
        or not all(isinstance(item, dict) for item in records)
    ):
        raise Problem(
            422,
            "validation_error",
            f"records must contain 1–{settings.ADMIN_API_MAX_BATCH} objects.",
        )
    return records


def plan_data(ctx: AdminContext, plan: DeletionPlan) -> dict[str, Any]:
    return {
        "counts": plan.counts,
        "blockers": plan.blockers,
        "permissions": sorted(plan.permissions),
        "allowed": plan.allowed,
        "confirmation": confirmation_token(ctx, plan),
        "effects": [
            {"model": item.model, "label": item.label, "action": item.action, "depth": item.depth}
            for item in plan.effects.values()
        ],
    }


async def delete_records(ctx: AdminContext, targets: list[Any], token: str | None) -> None:
    admin = RecordService(ctx).admin
    for target in targets:
        admin.check_permission(replace(ctx, obj=target), "delete", target)
    service = RecordService(ctx)
    plan = await service.plan_delete(targets, lock=True)
    if plan.permissions:
        raise Problem(
            403,
            "permission_denied",
            "Related records cannot be deleted.",
            plan=plan_data(ctx, plan),
        )
    if plan.blockers:
        raise Problem(
            409, "conflict", "Related records prevent deletion.", plan=plan_data(ctx, plan)
        )
    if any(effect.depth > 0 for effect in plan.effects.values()) and not confirmed(
        ctx, plan, token
    ):
        raise Problem(
            409,
            "confirmation_required",
            "Confirm deletion of related records.",
            plan=plan_data(ctx, plan),
        )
    async with ctx.db.begin_nested():
        for target in targets:
            await service.delete(target)


def register_records(router: APIRouter) -> None:
    register_meta(router)

    @router.get("/models/{label}/records", response_model=RecordPage)
    async def records(
        request: Request,
        label: str,
        user: USER,
        db: DB,
        q: str = "",
        filter: str | None = None,
        sort: str | None = None,
        page: int = 1,
        size: int = 50,
        fields: str | None = None,
        total: str = "none",
        view: str | None = None,
    ) -> Any:
        ctx = model_context(request, user, db, label)
        result = await list_records(ctx)
        links = []
        number = result["page"]["number"]
        if result["page"]["has_next"]:
            links.append(f'<{request.url.include_query_params(page=number + 1)}>; rel="next"')
        if number > 1:
            links.append(f'<{request.url.include_query_params(page=number - 1)}>; rel="prev"')
        return JSONResponse(result, headers={"Link": ", ".join(links)} if links else {})

    @router.post(
        "/models/{label}/records:query",
        response_model=RecordPage,
        openapi_extra=body_schema(QueryRequest),
    )
    async def query(request: Request, label: str, user: USER, db: DB) -> Any:
        return await list_records(model_context(request, user, db, label), await json_body(request))

    @router.get("/models/{label}/records/{pk}", response_model=RecordOutput)
    async def record(request: Request, label: str, pk: str, user: USER, db: DB) -> Response:
        ctx = model_context(request, user, db, label)
        obj = await get_record(ctx, pk)
        data = await representation(ctx, obj)
        return JSONResponse(data, headers={"ETag": data["meta"]["etag"]})

    @router.post(
        "/models/{label}/records",
        status_code=201,
        response_model=RecordOutput | dict[str, list[RecordOutput]],
        openapi_extra=body_schema(RecordInput, batch=True),
    )
    async def create(request: Request, label: str, user: USER, db: DB) -> Response:
        ctx = model_context(request, user, db, label)
        RecordService(ctx).admin.check_permission(ctx, "add")
        body = await json_body(request)

        async def operation() -> Response:
            batch = "records" in body
            items = batch_items(body) if batch else [body]
            output = []
            async with db.begin_nested():
                for index, item in enumerate(items):
                    try:
                        validate_keys(item, {"fields"})
                        obj = await save_record(ctx, item["fields"])
                        output.append(await representation(ctx, obj))
                    except Problem as exc:
                        if batch and exc.status == 422:
                            raise Problem(
                                422,
                                "validation_error",
                                "Invalid batch record.",
                                errors={
                                    str(index): exc.extra.get("errors", {"__all__": [exc.detail]})
                                },
                            ) from exc
                        raise
            if batch:
                return JSONResponse({"records": output}, status_code=201)
            data = output[0]
            return JSONResponse(
                data,
                status_code=201,
                headers={
                    "ETag": data["meta"]["etag"],
                    "Location": f"{request.url.path}/{quote(data['id'], safe='')}",
                },
            )

        async def authorize_replay(data: Any) -> None:
            for item in data.get("records", [data]):
                obj = await get_record(ctx, item["id"])
                current = await representation(ctx, obj)
                if item["fields"].keys() - current["fields"].keys():
                    raise Problem(404, "not_found", "The original response is unavailable.")
                for spec in api_specs(ctx):
                    if spec.type.name not in {"link", "multi_link"} or "target" not in spec.options:
                        continue
                    value = item["fields"].get(spec.name)
                    values = (
                        value if isinstance(value, list) else [value] if value is not None else []
                    )
                    key = related_key(spec)
                    pks = [key.type.python_type(value) for value in values]
                    visible = await related_objects(ctx, spec, pks=pks) if pks else []
                    if {str(getattr(row, key.key)) for row in visible} != set(values):
                        raise Problem(404, "not_found", "The original response is unavailable.")

        return await idempotency.execute(
            request,
            db,
            f"{ctx.site.name}:user:{user.id}",
            body,
            operation,
            authorize_replay=authorize_replay,
        )

    @router.patch(
        "/models/{label}/records/{pk}",
        response_model=RecordOutput,
        openapi_extra=body_schema(RecordInput),
    )
    async def update(request: Request, label: str, pk: str, user: USER, db: DB) -> Response:
        ctx = model_context(request, user, db, label)
        obj = await get_record(ctx, pk, lock=True)
        RecordService(ctx).admin.check_permission(ctx, "change", obj)
        current = await representation(ctx, obj)
        check_if_match(current["meta"]["etag"], request.headers.get("if-match"))
        body = await json_body(request)
        validate_keys(body, {"fields"})
        obj = await save_record(ctx, body["fields"], obj)
        data = await representation(ctx, obj)
        return JSONResponse(data, headers={"ETag": data["meta"]["etag"]})

    @router.patch(
        "/models/{label}/records",
        response_model=dict[str, list[RecordOutput]],
        openapi_extra=body_schema(UpdateInput, batch_only=True),
    )
    async def batch_update(request: Request, label: str, user: USER, db: DB) -> Any:
        ctx = model_context(request, user, db, label)
        items = batch_items(await json_body(request))
        ids = []
        for item in items:
            validate_keys(item, {"id", "fields"}, {"etag"})
            if not isinstance(item["id"], str) or not isinstance(item.get("etag", ""), str):
                raise Problem(422, "validation_error", "Record id and etag must be strings.")
            ids.append(item["id"])
        if len(set(ids)) != len(ids):
            raise Problem(422, "validation_error", "Duplicate record id.")
        targets = {pk: await get_record(ctx, pk, lock=True) for pk in sorted(ids)}
        output = []
        async with db.begin_nested():
            for index, item in enumerate(items):
                obj = targets[item["id"]]
                ctx.obj = obj
                RecordService(ctx).admin.check_permission(ctx, "change", obj)
                current = await representation(ctx, obj)
                check_if_match(current["meta"]["etag"], item.get("etag"))
                try:
                    obj = await save_record(ctx, item["fields"], obj)
                except Problem as exc:
                    if exc.status == 422:
                        raise Problem(
                            422,
                            "validation_error",
                            "Invalid batch record.",
                            errors={str(index): exc.extra.get("errors", {"__all__": [exc.detail]})},
                        ) from exc
                    raise
                output.append(await representation(ctx, obj))
        return {"records": output}

    @router.get("/models/{label}/records/{pk}/delete-preview")
    async def preview(request: Request, label: str, pk: str, user: USER, db: DB) -> Any:
        ctx = model_context(request, user, db, label)
        obj = await get_record(ctx, pk)
        RecordService(ctx).admin.check_permission(ctx, "delete", obj)
        return plan_data(ctx, await RecordService(ctx).plan_delete([obj]))

    @router.delete("/models/{label}/records/{pk}", status_code=204)
    async def delete(request: Request, label: str, pk: str, user: USER, db: DB) -> Response:
        ctx = model_context(request, user, db, label)
        obj = await get_record(ctx, pk, lock=True)
        RecordService(ctx).admin.check_permission(ctx, "delete", obj)
        data = await representation(ctx, obj)
        check_if_match(data["meta"]["etag"], request.headers.get("if-match"))
        await delete_records(ctx, [obj], request.headers.get("x-delete-confirmation"))
        return Response(status_code=204)

    @router.post(
        "/models/{label}/records:delete", status_code=204, openapi_extra=body_schema(DeleteInput)
    )
    async def batch_delete(request: Request, label: str, user: USER, db: DB) -> Response:
        ctx = model_context(request, user, db, label)
        RecordService(ctx).admin.check_permission(ctx, "delete")
        body = await json_body(request)
        validate_keys(body, {"ids"}, {"etags"})
        ids = body["ids"]
        if (
            not isinstance(ids, list)
            or not 1 <= len(ids) <= settings.ADMIN_API_MAX_BATCH
            or not all(isinstance(pk, str) for pk in ids)
            or len(set(ids)) != len(ids)
        ):
            raise Problem(422, "validation_error", "Invalid record ids.")
        etags = body.get("etags", {})
        if not isinstance(etags, dict) or not all(
            isinstance(value, str) for value in etags.values()
        ):
            raise Problem(422, "validation_error", "Invalid record etags.")

        async def operation() -> Response:
            targets = [await get_record(ctx, pk, lock=True) for pk in sorted(ids)]
            for obj in targets:
                ctx.obj = obj
                current = await representation(ctx, obj)
                check_if_match(
                    current["meta"]["etag"], etags.get(str(RecordService(ctx).admin.get_pk(obj)))
                )
            await delete_records(ctx, targets, request.headers.get("x-delete-confirmation"))
            return Response(status_code=204)

        return await idempotency.execute(
            request, db, f"{ctx.site.name}:user:{user.id}", body, operation
        )

    @router.get("/models/{label}/options/{field}")
    async def options(
        request: Request,
        label: str,
        field: str,
        user: USER,
        db: DB,
        q: str = Query(default="", max_length=200),
        page: int = 1,
    ) -> Any:
        ctx = model_context(request, user, db, label)
        spec = next((spec for spec in api_specs(ctx) if spec.name == field), None)
        if spec is None or spec.type.name not in {"single_select", "link", "multi_link"}:
            raise Problem(404, "not_found", "Field options are unavailable.")
        if page < 1 or len(q) > 200:
            raise Problem(400, "invalid_query", "Invalid options query.")
        offset = (page - 1) * 50
        if spec.type.name == "single_select":
            choices = [
                {"id": value, "label": label}
                for value, label in normalize_choices(spec.options.get("choices"))
                if q.casefold() in label.casefold()
            ]
            return {
                "options": choices[offset : offset + 50],
                "has_next": len(choices) > offset + 50,
            }
        admin, related_ctx = related_admin(ctx, spec)
        if not admin.has_view_permission(related_ctx):
            return {"options": [], "has_next": False}
        from obox_builder.fields import QuerySpec

        stmt = (
            QuerySpec(q=q)
            .apply(
                admin.get_queryset(related_ctx),
                admin.get_query_specs(related_ctx),
                search_specs=admin.get_search_specs(related_ctx),
            )
            .order_by(admin.pk_column())
            .offset(offset)
            .limit(51)
        )
        rows = (await db.scalars(stmt)).unique().all()
        key = related_key(spec)
        return {
            "options": [
                {"id": str(getattr(obj, key.key)), "label": str(obj)}
                for obj in rows[:50]
                if admin.has_view_permission(replace(related_ctx, obj=obj), obj)
            ],
            "has_next": len(rows) > 50,
        }

    @router.get("/models/{label}/records/{pk}/files/{field}")
    async def download(
        request: Request, label: str, pk: str, field: str, user: USER, db: DB
    ) -> Response:
        ctx = model_context(request, user, db, label)
        obj = await get_record(ctx, pk)
        spec = next(
            (spec for spec in api_specs(ctx) if spec.name == field and spec.type.name == "file"),
            None,
        )
        path = resolve_upload(str(field_value(obj, spec))) if spec is not None else None
        if path is None:
            raise Problem(404, "not_found", "File is unavailable.")
        return FileResponse(path, filename=path.name)
