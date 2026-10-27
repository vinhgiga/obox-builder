"""Bounded query parsing, saved-view composition and stable pagination."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from obox_builder.contrib.admin.api.auth import parse_body
from obox_builder.contrib.admin.api.problems import Problem
from obox_builder.contrib.admin.api.serializers import serialize_records
from obox_builder.contrib.admin.context import AdminContext
from obox_builder.contrib.admin.list_query import load_query, view_scope
from obox_builder.contrib.admin.models import SavedView
from obox_builder.contrib.admin.services import RecordService
from obox_builder.fields import QuerySpec


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filter: dict[str, Any] | None = None
    sort: str | list[str] | None = None
    q: str = Field(default="", max_length=200)
    page: int = Field(default=1, ge=1)
    size: int = Field(default=50, ge=1, le=200)
    fields: str | list[str] | None = None
    total: Literal["none", "capped", "exact"] = "none"
    view: str | int | None = None


async def list_records(ctx: AdminContext, body: dict[str, Any] | None = None) -> dict[str, Any]:
    admin = ctx.admin
    assert admin is not None  # noqa: S101
    params: dict[str, Any] = dict(ctx.request.query_params)
    if body is None:
        query, _view = await load_query(ctx)
    else:
        parsed = parse_body(QueryRequest, body)
        overrides = parsed.model_dump(exclude_unset=True)
        params.update(overrides)
        query_params: dict[str, Any] = {}
        view_id = params.get("view")
        if view_id is not None:
            try:
                pk = int(view_id)
            except (TypeError, ValueError) as exc:
                raise Problem(400, "invalid_query", "Invalid saved view.") from exc
            view = await ctx.db.scalar(view_scope(ctx).where(SavedView.id == pk))
            if view is None:
                raise Problem(404, "not_found", "Saved view is unavailable.")
            query_params.update(view.config)
        query_params.update(
            {
                name: value
                for name, value in params.items()
                if name in {"filter", "sort", "q", "page", "size"} and value is not None
            }
        )
        if isinstance(query_params.get("filter"), dict):
            query_params["filter"] = json.dumps(query_params["filter"])
        query = QuerySpec.parse(
            query_params,
            admin.get_query_specs(ctx),
            default_sort=admin.get_ordering(ctx),
            default_size=admin.get_list_per_page(ctx),
        )
    total_mode = params.get("total", "none")
    if total_mode not in {"none", "capped", "exact"}:
        raise Problem(400, "invalid_query", "Invalid total mode.")
    fields = params.get("fields")
    selected = (
        [name.strip() for name in fields.split(",") if name.strip()]
        if isinstance(fields, str)
        else fields
    )
    page = await RecordService(ctx).list(query, total=total_mode)
    count = page.count
    objects = [obj for obj in page.objects if admin.has_view_permission(replace(ctx, obj=obj), obj)]
    records, included = await serialize_records(ctx, objects, selected)
    return {
        "records": records,
        "page": {
            "number": query.page,
            "size": query.size,
            "has_next": page.has_next,
            "total": min(count, 10_000) if count is not None and total_mode == "capped" else count,
            "total_capped": count is not None and total_mode == "capped" and count > 10_000,
        },
        "included": included,
    }
