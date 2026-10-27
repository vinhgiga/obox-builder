"""Save, update and delete the current user's private model views."""

from __future__ import annotations

import json
from urllib.parse import urlencode

from fastapi import HTTPException
from fastapi.responses import RedirectResponse

from obox_builder.contrib.admin.context import AdminContext
from obox_builder.contrib.admin.list_query import load_query, view_scope
from obox_builder.contrib.admin.models import SavedView
from obox_builder.contrib.admin.views.common import ensure_csrf
from obox_builder.fields import QuerySpecError


async def save_view(ctx: AdminContext) -> RedirectResponse:
    assert ctx.admin is not None  # noqa: S101
    form = await ctx.request.form()
    await ensure_csrf(ctx.request, str(form.get("csrf_token", "")))
    name = str(form.get("name", "")).strip()
    if not 1 <= len(name) <= 100:
        raise HTTPException(400, "View name must contain 1–100 characters.")
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
    if selected is not None and form.get("save_as") != "new":
        selected.name, selected.config = name, config
    else:
        selected = SavedView(
            owner_id=ctx.user.id,
            site_name=ctx.site.name,
            model_label=ctx.admin.model_label,
            name=name,
            config=config,
        )
        ctx.db.add(selected)
    await ctx.db.flush()
    return RedirectResponse(f"{ctx.admin.list_path()}?view={selected.id}", status_code=303)


async def delete_view(ctx: AdminContext, pk: int) -> RedirectResponse:
    assert ctx.admin is not None  # noqa: S101
    form = await ctx.request.form()
    await ensure_csrf(ctx.request, str(form.get("csrf_token", "")))
    selected = await ctx.db.scalar(view_scope(ctx).where(SavedView.id == pk))
    if selected is None:
        raise HTTPException(404, "Saved view does not exist.")
    await ctx.db.delete(selected)
    await ctx.db.flush()
    return RedirectResponse(ctx.admin.list_path(), status_code=303)


async def edit_view(ctx: AdminContext, pk: int, *, duplicate: bool = False) -> RedirectResponse:
    assert ctx.admin is not None  # noqa: S101
    form = await ctx.request.form()
    await ensure_csrf(ctx.request, str(form.get("csrf_token", "")))
    selected = await ctx.db.scalar(view_scope(ctx).where(SavedView.id == pk).with_for_update())
    if selected is None:
        raise HTTPException(404, "Saved view does not exist.")
    if duplicate:
        selected = SavedView(
            owner_id=ctx.user.id,
            site_name=ctx.site.name,
            model_label=ctx.admin.model_label,
            name=f"{selected.name[:95]} copy",
            config=dict(selected.config),
        )
        ctx.db.add(selected)
    else:
        name = str(form.get("name", "")).strip()
        if not 1 <= len(name) <= 100:
            raise HTTPException(400, "View name must contain 1–100 characters.")
        selected.name = name
    await ctx.db.flush()
    params = (
        []
        if duplicate
        else [
            (key, value) for key, value in ctx.request.query_params.multi_items() if key != "view"
        ]
    )
    params.insert(0, ("view", str(selected.id)))
    return RedirectResponse(f"{ctx.admin.list_path()}?{urlencode(params)}", status_code=303)
