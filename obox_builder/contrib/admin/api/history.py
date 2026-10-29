"""Scoped audit timelines and reversals through the normal write hooks."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from obox_builder.contrib.admin.api.deps import DB, USER, json_body
from obox_builder.contrib.admin.api.etag import check_if_match
from obox_builder.contrib.admin.api.meta import model_context
from obox_builder.contrib.admin.api.problems import Problem
from obox_builder.contrib.admin.api.records import (
    body_schema,
    delete_records,
    get_record,
    representation,
    validate_keys,
)
from obox_builder.contrib.admin.api.serializers import api_specs, record_values
from obox_builder.contrib.admin.services import RecordService
from obox_builder.contrib.admin.services.history import (
    DiffMask,
    diff_mask,
    entry_view,
    timeline_available,
)
from obox_builder.contrib.admin.services.revert import OP_PERMISSION, blocked_fields, build_plan
from obox_builder.contrib.history import HISTORY_RESTORE_PERMISSION
from obox_builder.contrib.history.context import CTX_KEY, from_request
from obox_builder.contrib.history.models import RowChange
from obox_builder.contrib.history.service import FeedFilters, feed_entries, history_entries
from obox_builder.forms import build_schema


class RevertInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    reason: str = Field(default="", max_length=500)
    force: bool = False


async def history_context(
    request: Request, user: Any, db: Any, label: str, pk: str, *, lock: bool = False
) -> Any:
    ctx = model_context(request, user, db, label)
    admin = ctx.admin
    assert admin is not None  # noqa: S101
    if not timeline_available(admin):
        raise Problem(404, "not_found", "History is unavailable.")
    try:
        await get_record(ctx, pk, lock=lock)
    except LookupError:
        # Distinguish a deleted row from an existing row hidden by the queryset.
        exists = await db.get(admin.model, admin.parse_pk(pk))
        if exists is not None or not user.is_superuser:
            raise Problem(404, "not_found", "Record is unavailable.") from None
        ctx.obj = None
    if not admin.has_history_permission(ctx, ctx.obj):
        raise Problem(403, "permission_denied", "You cannot view history.")
    return ctx


def history_mask(ctx: Any) -> DiffMask:
    base = diff_mask(ctx.admin.model, admin=ctx.admin, user=ctx.user, ctx=ctx)
    specs = api_specs(ctx)
    values = record_values(ctx.obj, specs) if ctx.obj is not None else {}
    record = ctx.record_context(values)
    visible = {spec.name for spec in specs if spec.show_if is None or spec.show_if(record)}
    return replace(base, visible=base.visible & visible, is_superuser=False)


def history_data(ctx: Any, entry: Any) -> dict[str, Any]:
    result = entry_view(entry, mask=history_mask(ctx))
    result["id"] = str(entry.id)
    result["change_set_id"] = str(entry.change_set_id)
    specs = {spec.name: spec for spec in ctx.admin.get_form_specs(ctx)}
    result["revertable"] = (
        ctx.user.has_perm(HISTORY_RESTORE_PERMISSION)
        and RecordService(ctx).allowed(OP_PERMISSION[entry.op], ctx.obj)
        and not blocked_fields(entry, specs, mask=history_mask(ctx))
        and (
            (entry.op == "delete" and ctx.obj is None)
            or (entry.op != "delete" and ctx.obj is not None)
        )
    )
    return result


def register_history(router: APIRouter) -> None:
    base = "/models/{label}/records/{pk}/history"

    @router.get(base)
    async def timeline(
        request: Request,
        label: str,
        pk: str,
        user: USER,
        db: DB,
        before: int | None = Query(None, ge=1),
        limit: int = Query(20, ge=1, le=100),
    ) -> Any:
        ctx = await history_context(request, user, db, label, pk)
        entries = await history_entries(
            db, ctx.admin.model, ctx.admin.parse_pk(pk), before=before, limit=limit + 1
        )
        return {
            "entries": [history_data(ctx, row) for row in entries[:limit]],
            "before": str(entries[limit - 1].id) if len(entries) > limit else None,
        }

    @router.get("/history")
    async def feed(
        request: Request,
        user: USER,
        db: DB,
        before: int | None = Query(None, ge=1),
        limit: int = Query(20, ge=1, le=100),
        model: str | None = None,
        actor: str = Query("", max_length=150),
        source: str = "",
        op: str = "",
        since: AwareDatetime | None = None,
        until: AwareDatetime | None = None,
    ) -> Any:
        clauses = []
        for label, admin in request.state.admin_site.get_registry().items():
            if model is not None and model != label:
                continue
            try:
                ctx = model_context(request, user, db, label)
            except Problem:
                continue
            if not timeline_available(admin) or not admin.has_history_permission(ctx):
                continue
            key = sa.cast(admin.pk_column(), sa.String)
            visible = admin.get_queryset(ctx).with_only_columns(key).order_by(None)
            allowed: sa.ColumnElement[bool] = RowChange.object_pk.in_(visible)
            if user.is_superuser:
                allowed = sa.or_(allowed, RowChange.object_pk.not_in(sa.select(key)))
            clauses.append(sa.and_(RowChange.model_label == label, allowed))
        if source and source not in {"admin_ui", "api", "cli", "job", "system", "import", "revert"}:
            raise Problem(400, "invalid_query", "Invalid history source.")
        if op and op not in {"create", "update", "delete"}:
            raise Problem(400, "invalid_query", "Invalid history operation.")
        if since is not None and until is not None and since >= until:
            raise Problem(400, "invalid_query", "History end must follow its start.")
        filters = FeedFilters(
            since=since,
            until=until,
            actor=actor,
            source=source,
            op=op,
            extra_conditions=(sa.or_(sa.false(), *clauses),),
        )
        entries = await feed_entries(db, filters=filters, before=before, limit=limit + 1)
        result = []
        for entry in entries[:limit]:
            try:
                ctx = await history_context(request, user, db, entry.model_label, entry.object_pk)
            except (Problem, LookupError):
                continue
            result.append(history_data(ctx, entry))
        return {
            "entries": result,
            "before": str(entries[limit - 1].id) if len(entries) > limit else None,
        }

    @router.post(base + "/{entry_id}/revert", openapi_extra=body_schema(RevertInput))
    async def revert(
        request: Request, label: str, pk: str, entry_id: int, user: USER, db: DB
    ) -> Any:
        ctx = await history_context(request, user, db, label, pk, lock=True)
        admin = ctx.admin
        data = await json_body(request)
        validate_keys(data, set(), {"reason", "force"})
        reason, force = data.get("reason", ""), data.get("force", False)
        if not isinstance(reason, str) or len(reason) > 500 or not isinstance(force, bool):
            raise Problem(422, "validation_error", "Invalid revert options.")
        if not user.has_perm(HISTORY_RESTORE_PERMISSION):
            raise Problem(403, "permission_denied", "You cannot restore history.")
        plan = await build_plan(ctx, admin, admin.parse_pk(pk), entry_id, mask=history_mask(ctx))
        service = RecordService(ctx)
        service.check_permission(OP_PERMISSION[plan.entry.op], plan.target)
        if plan.target is not None:
            check_if_match(
                (await representation(ctx, plan.target))["meta"]["etag"],
                request.headers.get("if-match"),
            )
        if plan.blocked:
            raise Problem(
                409,
                "revert_blocked",
                "The recorded state cannot be restored.",
                errors={item.name or "__all__": [item.reason] for item in plan.blocked},
            )
        if plan.conflicts and not force:
            raise Problem(409, "conflict", "The record changed after this history entry.")
        db.info[CTX_KEY] = replace(
            from_request(request, source="api"), reverts_id=plan.entry.id, reason=reason
        )
        if plan.noop:
            return {"id": pk, "reverted": False}
        if plan.entry.op == "create":
            await delete_records(ctx, [plan.target], request.headers.get("x-delete-confirmation"))
            return {"id": pk, "deleted": True}
        # Recorded read-only values are trusted, but dynamic field rules still apply.
        specs = [
            replace(spec, readonly=False, editable=True)
            for spec in admin.get_form_specs(ctx)
            if spec.name in plan.values and not spec.secret
        ]
        bound = await service.bind(
            plan.values,
            target=plan.target,
            specs=specs,
            schema=build_schema(specs, name="ApiRevert", mode="revert"),
            partial=True,
        )
        if not bound.is_valid or set(plan.values) - bound.cleaned.keys():
            raise Problem(
                422,
                "validation_error",
                "Recorded values no longer pass validation.",
                errors=bound.errors,
            )
        target = await service.save(bound, target=plan.target, primary_key=plan.pk_value)
        await db.refresh(target)
        result = await representation(ctx, await service.get(str(admin.get_pk(target))))
        return JSONResponse(jsonable_encoder(result), headers={"ETag": result["meta"]["etag"]})
