"""Explicitly exposed actions over bounded, authorized selections."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from obox_builder.conf import settings
from obox_builder.contrib.admin.api import idempotency
from obox_builder.contrib.admin.api.auth import parse_body
from obox_builder.contrib.admin.api.deps import DB, USER, json_body, principal
from obox_builder.contrib.admin.api.meta import model_context
from obox_builder.contrib.admin.api.problems import Problem
from obox_builder.contrib.admin.context import AdminContext
from obox_builder.contrib.admin.services import RecordService
from obox_builder.contrib.auth.tokens import scope_allows
from obox_builder.fields import QuerySpec


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[str] = Field(default_factory=list)
    select_across: bool = False
    filter: dict[str, Any] | None = None
    q: str = Field(default="", max_length=200)


def api_actions(ctx: AdminContext) -> list[Any]:
    admin = RecordService(ctx).admin
    return [spec for spec in admin.get_actions(ctx) if spec.api and action_allowed(ctx, spec)]


def action_allowed(ctx: AdminContext, spec: Any, obj: Any = None) -> bool:
    admin = RecordService(ctx).admin
    scopes = ctx.extra.get("api_scopes")
    if not spec.permissions:
        return scopes is None or scope_allows(scopes, admin.model_label, "write")
    for permission in spec.permissions:
        operation = {"view": "read", "delete": "delete"}.get(permission, "write")
        checker = getattr(admin, f"has_{permission}_permission", None)
        if (
            checker is not None
            and (scopes is None or scope_allows(scopes, admin.model_label, operation))
            and (checker(ctx) if permission == "add" else checker(ctx, obj))
        ):
            return True
    return False


def register_actions(router: APIRouter) -> None:
    @router.post(
        "/models/{label}/actions/{name}",
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": Selection.model_json_schema()}},
            }
        },
    )
    async def run(request: Request, label: str, name: str, user: USER, db: DB) -> Response:
        ctx = model_context(request, user, db, label)
        admin = RecordService(ctx).admin
        spec = next((spec for spec in api_actions(ctx) if spec.name == name), None)
        if spec is None:
            raise Problem(404, "not_found", "Action is unavailable.")
        body = await json_body(request)
        selection = parse_body(Selection, body)
        if selection.select_across == bool(selection.ids):
            raise Problem(422, "validation_error", "Choose ids or select_across.")
        if len(selection.ids) > settings.API_MAX_BATCH or len(set(selection.ids)) != len(
            selection.ids
        ):
            raise Problem(422, "validation_error", "Invalid action selection.")
        query = QuerySpec.parse(
            {"filter": json.dumps(selection.filter) if selection.filter else "", "q": selection.q},
            admin.get_query_specs(ctx),
        )
        stmt = query.apply(
            admin.get_queryset(ctx),
            admin.get_query_specs(ctx),
            search_specs=admin.get_search_specs(ctx),
            text_collation=settings.ADMIN_TEXT_COLLATION,
        )
        ids = [admin.parse_pk(pk) for pk in selection.ids]
        if ids:
            stmt = stmt.where(admin.pk_column().in_(ids))
        objects = (
            (
                await db.scalars(
                    stmt.order_by(None)
                    .order_by(admin.pk_column())
                    .limit(settings.API_MAX_BATCH + 1)
                    .with_for_update(of=admin.model)
                    .execution_options(populate_existing=True)
                )
            )
            .unique()
            .all()
        )
        if len(objects) > settings.API_MAX_BATCH:
            raise Problem(413, "payload_too_large", "Narrow the action selection.")
        if ids and {admin.get_pk(obj) for obj in objects} != set(ids):
            raise Problem(404, "not_found", "A selected record is unavailable.")
        for obj in objects:
            row_ctx = replace(ctx, obj=obj)
            if not admin.has_view_permission(row_ctx, obj) or not action_allowed(
                row_ctx, spec, obj
            ):
                raise Problem(403, "permission_denied", "A selected record cannot be changed.")
        scoped = admin.get_queryset(ctx).where(
            admin.pk_column().in_([admin.get_pk(obj) for obj in objects])
        )

        async def operation() -> Response:
            async with db.begin_nested():
                result = await spec.func(ctx, scoped)
                await db.flush()
                if isinstance(result, Response):
                    if result.status_code == 204:
                        return result
                    if result.media_type != "application/json" or result.status_code >= 400:
                        raise Problem(
                            422,
                            "invalid_action_response",
                            "API actions must return JSON data or a successful JSON response.",
                        )
                    return result
                return JSONResponse({"affected": len(objects), "result": jsonable_encoder(result)})

        return await idempotency.execute(request, db, principal(request), body, operation)
