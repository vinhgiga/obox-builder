"""HTML saved-query controls call the resource's private-view operations."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode

from fastapi import HTTPException
from fastapi.responses import RedirectResponse

from obox_builder.contrib.admin.context import AdminContext
from obox_builder.contrib.admin.list_query import load_query
from obox_builder.contrib.admin.views.common import ensure_csrf
from obox_builder.fields import QuerySpecError
from obox_builder.resources import ResourceValidationError


async def execute(ctx: AdminContext, operation: str, data: dict[str, Any]) -> Any:
    if ctx.admin is None:
        raise ValueError("A model view is required.")
    ctx.admin.check_permission(ctx, "view")
    try:
        return await ctx.admin.resource.execute(operation, ctx.operation_context(), data)
    except (ResourceValidationError, QuerySpecError) as exc:
        raise HTTPException(400, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


async def save_view(ctx: AdminContext) -> RedirectResponse:
    assert ctx.admin is not None  # noqa: S101
    form = await ctx.request.form()
    await ensure_csrf(ctx.request, str(form.get("csrf_token", "")))
    try:
        query, selected = await load_query(ctx)
    except QuerySpecError as exc:
        raise HTTPException(400, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    config = query.to_params()
    config.pop("page", None)
    config.pop("f", None)
    config["filter"] = json.dumps(query.combined_filters().to_dict(), ensure_ascii=False)
    config.setdefault("sort", "")
    config.setdefault("group", "")
    data = {"name": str(form.get("name", "")).strip(), "config": config}
    if selected is not None and form.get("save_as") != "new":
        data["id"] = selected.id
    result = await execute(ctx, "saveView", data)
    return RedirectResponse(f"{ctx.admin.list_path()}?view={result.id}", status_code=303)


async def delete_view(ctx: AdminContext, pk: int) -> RedirectResponse:
    assert ctx.admin is not None  # noqa: S101
    form = await ctx.request.form()
    await ensure_csrf(ctx.request, str(form.get("csrf_token", "")))
    await execute(ctx, "deleteView", {"id": str(pk)})
    return RedirectResponse(ctx.admin.list_path(), status_code=303)


async def edit_view(ctx: AdminContext, pk: int, *, duplicate: bool = False) -> RedirectResponse:
    assert ctx.admin is not None  # noqa: S101
    form = await ctx.request.form()
    await ensure_csrf(ctx.request, str(form.get("csrf_token", "")))
    data = {"id": str(pk)}
    if not duplicate:
        data["name"] = str(form.get("name", "")).strip()
    result = await execute(ctx, "duplicateView" if duplicate else "renameView", data)
    params = (
        []
        if duplicate
        else [
            (key, value) for key, value in ctx.request.query_params.multi_items() if key != "view"
        ]
    )
    params.insert(0, ("view", result.id))
    return RedirectResponse(f"{ctx.admin.list_path()}?{urlencode(params)}", status_code=303)
