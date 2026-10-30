"""Private saved query configurations shared with the HTML admin."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from obox_builder.contrib.admin.api.auth import parse_body
from obox_builder.contrib.admin.api.deps import DB, USER, json_body
from obox_builder.contrib.admin.api.meta import model_context
from obox_builder.contrib.admin.api.problems import Problem
from obox_builder.contrib.admin.list_query import view_scope
from obox_builder.contrib.savedviews.models import SavedView
from obox_builder.contrib.admin.services import RecordService
from obox_builder.contrib.auth.tokens import scope_allows
from obox_builder.fields import QuerySpec


class ViewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    config: dict[str, Any]
    id: str | None = None


def view_data(row: SavedView) -> dict[str, Any]:
    return {"id": str(row.id), "name": row.name, "config": row.config}


def check_write(ctx: Any) -> None:
    scopes = ctx.extra.get("api_scopes")
    if scopes is not None and not scope_allows(scopes, ctx.admin.model_label, "write"):
        raise Problem(403, "permission_denied", "Writing views is outside the token scope.")


def register_views(router: APIRouter) -> None:
    @router.get("/models/{label}/views")
    async def listing(request: Request, label: str, user: USER, db: DB) -> Any:
        ctx = model_context(request, user, db, label)
        rows = (await db.scalars(view_scope(ctx).order_by(SavedView.name, SavedView.id))).all()
        return {"views": [view_data(row) for row in rows]}

    @router.post(
        "/models/{label}/views",
        status_code=201,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": ViewInput.model_json_schema()}},
            }
        },
    )
    async def create(request: Request, response: Response, label: str, user: USER, db: DB) -> Any:
        ctx = model_context(request, user, db, label)
        check_write(ctx)
        admin = RecordService(ctx).admin
        data = parse_body(ViewInput, await json_body(request))
        if not data.name.strip():
            raise Problem(422, "validation_error", "View name cannot be blank.")
        if data.config.keys() - {"filter", "f", "sort", "group", "q", "size"}:
            raise Problem(400, "invalid_query", "Unknown saved query property.")
        params = dict(data.config)
        if isinstance(params.get("filter"), dict):
            params["filter"] = json.dumps(params["filter"])
        # Saved query values use the same wire types as the list query parser.
        if any(not isinstance(value, (str, list, int)) for value in params.values()) or any(
            not isinstance(value, str) for name, value in params.items() if name in {"q", "filter"}
        ):
            raise Problem(400, "invalid_query", "Invalid saved query values.")
        config = QuerySpec.parse(params, admin.get_query_specs(ctx)).to_params()
        row: SavedView | None
        if data.id is None:
            row = SavedView(
                owner_id=user.id,
                site_name=ctx.site.name,
                model_label=label,
                name=data.name.strip(),
                config=config,
            )
            db.add(row)
        else:
            try:
                pk = int(data.id)
            except ValueError as exc:
                raise Problem(422, "validation_error", "Invalid saved view id.") from exc
            row = await db.scalar(view_scope(ctx).where(SavedView.id == pk).with_for_update())
            if row is None:
                raise Problem(404, "not_found", "Saved view is unavailable.")
            row.name, row.config = data.name.strip(), config
            response.status_code = 200
        await db.flush()
        return view_data(row)

    @router.delete("/models/{label}/views/{pk}", status_code=204)
    async def delete(request: Request, label: str, pk: int, user: USER, db: DB) -> Response:
        ctx = model_context(request, user, db, label)
        check_write(ctx)
        row = await db.scalar(view_scope(ctx).where(SavedView.id == pk).with_for_update())
        if row is None:
            raise Problem(404, "not_found", "Saved view is unavailable.")
        await db.delete(row)
        return Response(status_code=204)
