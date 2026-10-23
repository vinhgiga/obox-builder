"""Generic CRUD views for registered models (full-page, server-rendered).

One set of endpoints is generated per registered ModelAdmin at router build
time, so every model gets list/add/change/delete with the same behaviour and
the OpenAPI schema (M9) can enumerate them.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlencode

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from obox_builder.conf import settings
from obox_builder.contrib.admin.context import AdminContext
from obox_builder.contrib.admin.list_query import group_headers, load_query, view_scope
from obox_builder.contrib.admin.models import SavedView
from obox_builder.contrib.admin.options import ModelAdmin
from obox_builder.contrib.admin.site import AdminSite
from obox_builder.contrib.admin.views.common import (
    ensure_csrf,
    flash,
    page_context,
)
from obox_builder.contrib.admin.views.history import (
    RevertLinks,
    before_cursor,
    build_card,
    diff_mask,
    timeline_available,
    timeline_context,
)
from obox_builder.contrib.admin.views.revert import OP_PERMISSION, revert_entry
from obox_builder.contrib.auth.dependencies import permission_required
from obox_builder.contrib.history import HISTORY_RESTORE_PERMISSION, HISTORY_VIEW_PERMISSION
from obox_builder.contrib.history.service import RowChangeEntry, entry_count
from obox_builder.db.session import get_db
from obox_builder.fields import FieldSpec, QuerySpec, QuerySpecError
from obox_builder.forms import BoundForm, render_values
from obox_builder.templating import render

logger = logging.getLogger(__name__)

COUNT_CAP = 10_000
LINK_CHOICE_LIMIT = 500

__all__ = ["register_model_routes"]


def register_model_routes(router: APIRouter, site: AdminSite) -> None:
    for label in sorted(site.get_registry()):
        admin = site.get_registry()[label]
        _register_one(router, site, admin)


def _register_one(router: APIRouter, site: AdminSite, admin: ModelAdmin) -> None:
    # Paths are relative to the mount point (``/admin``); ModelAdmin.list_path()
    # returns the absolute path used in links and redirects.
    base = f"/{admin.path_prefix}"
    if admin.imports_available():
        from obox_builder.contrib.admin.views.imports import register_import_routes

        register_import_routes(router, site, admin)
    name = admin.route_prefix
    view_dep = Depends(permission_required(admin.permission_name("view")))
    add_dep = Depends(permission_required(admin.permission_name("add")))
    change_dep = Depends(permission_required(admin.permission_name("change")))
    delete_dep = Depends(permission_required(admin.permission_name("delete")))

    async def list_view(
        request: Request, user: Any = view_dep, db: AsyncSession = Depends(get_db)
    ) -> Any:
        ctx = AdminContext(request=request, user=user, db=db, site=site, admin=admin)
        return await _list(ctx)

    async def add_view(
        request: Request, user: Any = add_dep, db: AsyncSession = Depends(get_db)
    ) -> Any:
        ctx = AdminContext(request=request, user=user, db=db, site=site, admin=admin)
        return await _render_form(ctx)

    async def add_submit(
        request: Request, user: Any = add_dep, db: AsyncSession = Depends(get_db)
    ) -> Any:
        ctx = AdminContext(request=request, user=user, db=db, site=site, admin=admin)
        return await _create(ctx)

    async def inline_submit(
        request: Request, user: Any = change_dep, db: AsyncSession = Depends(get_db)
    ) -> Any:
        ctx = AdminContext(request=request, user=user, db=db, site=site, admin=admin)
        return await _inline(ctx)

    async def actions_submit(
        request: Request, user: Any = view_dep, db: AsyncSession = Depends(get_db)
    ) -> Any:
        ctx = AdminContext(request=request, user=user, db=db, site=site, admin=admin)
        return await _actions(ctx)

    async def change_view(
        request: Request, pk: str, user: Any = change_dep, db: AsyncSession = Depends(get_db)
    ) -> Any:
        ctx = AdminContext(request=request, user=user, db=db, site=site, admin=admin)
        try:
            target = await _get_object(ctx, pk)
        except LookupError as exc:
            return _not_found(ctx, str(exc))
        readonly = not admin.has_change_permission(ctx, target)
        specs = admin.get_form_specs()
        return await _render_form(
            ctx, target=target, readonly=readonly, values=render_values(target, specs)
        )

    async def change_submit(
        request: Request, pk: str, user: Any = change_dep, db: AsyncSession = Depends(get_db)
    ) -> Any:
        ctx = AdminContext(request=request, user=user, db=db, site=site, admin=admin)
        try:
            target = await _get_object(ctx, pk)
        except LookupError as exc:
            return _not_found(ctx, str(exc))
        return await _update(ctx, target)

    async def delete_view(
        request: Request, pk: str, user: Any = delete_dep, db: AsyncSession = Depends(get_db)
    ) -> Any:
        ctx = AdminContext(request=request, user=user, db=db, site=site, admin=admin)
        try:
            target = await _get_object(ctx, pk)
        except LookupError as exc:
            return _not_found(ctx, str(exc))
        context = await page_context(request, admin=admin, target=target, error="")
        return render(request, "admin/model/delete.html", context)

    async def delete_submit(
        request: Request, pk: str, user: Any = delete_dep, db: AsyncSession = Depends(get_db)
    ) -> Any:
        ctx = AdminContext(request=request, user=user, db=db, site=site, admin=admin)
        try:
            target = await _get_object(ctx, pk)
        except LookupError as exc:
            return _not_found(ctx, str(exc))
        return await _delete(ctx, target)

    if admin.comments_available():
        from obox_builder.contrib.admin.views.comments import register_comment_routes

        register_comment_routes(router, site, admin)

    if timeline_available(admin):
        history_perm_dep = Depends(permission_required(HISTORY_VIEW_PERMISSION))

        async def history_view(
            request: Request,
            pk: str,
            user: Any = view_dep,
            _history_perm: Any = history_perm_dep,
            db: AsyncSession = Depends(get_db),
        ) -> Any:
            ctx = AdminContext(request=request, user=user, db=db, site=site, admin=admin)
            try:
                pk_value = admin.parse_pk(pk)
            except LookupError as exc:
                return _not_found(ctx, str(exc))
            target = await db.get(admin.model, pk_value)
            if target is None and await entry_count(db, admin.model, pk_value) == 0:
                return _not_found(ctx, f"{admin.name} with id {pk!r} does not exist.")
            return await _history(ctx, target, pk_value)

        router.add_api_route(
            f"{base}/{{pk}}/history",
            history_view,
            methods=["GET"],
            name=f"{name}_history",
        )

        restore_perm_dep = Depends(permission_required(HISTORY_RESTORE_PERMISSION))

        async def revert_submit(
            request: Request,
            pk: str,
            entry_id: int,
            user: Any = view_dep,
            _restore_perm: Any = restore_perm_dep,
            db: AsyncSession = Depends(get_db),
        ) -> Any:
            ctx = AdminContext(request=request, user=user, db=db, site=site, admin=admin)
            return await revert_entry(ctx, pk=pk, entry_id=entry_id)

        router.add_api_route(
            f"{base}/{{pk}}/history/{{entry_id}}/revert",
            revert_submit,
            methods=["POST"],
            name=f"{name}_revert",
        )

    async def view_save(
        request: Request, user: Any = view_dep, db: AsyncSession = Depends(get_db)
    ) -> Any:
        from obox_builder.contrib.admin.views.saved_views import save_view

        ctx = AdminContext(request=request, user=user, db=db, site=site, admin=admin)
        return await save_view(ctx)

    async def view_delete(
        request: Request, view_id: int, user: Any = view_dep, db: AsyncSession = Depends(get_db)
    ) -> Any:
        from obox_builder.contrib.admin.views.saved_views import delete_view

        ctx = AdminContext(request=request, user=user, db=db, site=site, admin=admin)
        return await delete_view(ctx, view_id)

    router.add_api_route(f"{base}/views", view_save, methods=["POST"], name=f"{name}_view_save")
    router.add_api_route(
        f"{base}/views/{{view_id}}/delete",
        view_delete,
        methods=["POST"],
        name=f"{name}_view_delete",
    )
    router.add_api_route(base, list_view, methods=["GET"], name=f"{name}_list")
    router.add_api_route(f"{base}/add", add_view, methods=["GET"], name=f"{name}_add")
    router.add_api_route(f"{base}/add", add_submit, methods=["POST"], name=f"{name}_add_submit")
    router.add_api_route(f"{base}/inline", inline_submit, methods=["POST"], name=f"{name}_inline")
    router.add_api_route(
        f"{base}/actions", actions_submit, methods=["POST"], name=f"{name}_actions"
    )
    router.add_api_route(f"{base}/{{pk}}", change_view, methods=["GET"], name=f"{name}_change")
    router.add_api_route(
        f"{base}/{{pk}}", change_submit, methods=["POST"], name=f"{name}_change_submit"
    )
    router.add_api_route(
        f"{base}/{{pk}}/delete", delete_view, methods=["GET"], name=f"{name}_delete"
    )
    router.add_api_route(
        f"{base}/{{pk}}/delete", delete_submit, methods=["POST"], name=f"{name}_delete_submit"
    )


# -- list -------------------------------------------------------------------


async def _list(
    ctx: AdminContext,
    *,
    inline_values: Mapping[tuple[str, str], str] | None = None,
    inline_errors: Mapping[tuple[str, str], str] | None = None,
    banner: str = "",
) -> HTMLResponse:
    admin = _require_admin(ctx)
    request = ctx.request
    specs = {spec.name: spec for spec in admin.get_field_specs()}
    try:
        query, selected_view = await load_query(ctx)
    except QuerySpecError as exc:
        return _bad_request(ctx, str(exc))
    except LookupError as exc:
        return _not_found(ctx, str(exc))

    stmt = admin.get_queryset(ctx)
    try:
        stmt = query.apply(
            stmt,
            specs,
            search_specs=admin.get_search_specs(),
            text_collation=settings.ADMIN_TEXT_COLLATION,
        )
    except QuerySpecError as exc:
        return _bad_request(ctx, str(exc))

    total = await ctx.db.scalar(
        sa.select(sa.func.count()).select_from(stmt.order_by(None).subquery())
    )
    total = int(total or 0)
    offset = (query.page - 1) * query.size
    result = await ctx.db.execute(stmt.limit(query.size).offset(offset))
    objects = result.scalars().unique().all()

    headers = await group_headers(ctx, stmt, query, specs, objects)
    columns = admin.get_list_specs()
    can_inline = bool(admin.get_inline_specs()) and admin.has_change_permission(ctx)
    if can_inline:
        edits = _inline_edits(admin, columns, objects, inline_values, inline_errors)
    else:
        edits = [[None] * len(columns) for _ in objects]
    has_history_ui = timeline_available(admin)
    has_comments_ui = admin.comments_available()
    comment_counts: dict[str, int] = {}
    if has_comments_ui:
        from obox_builder.contrib.comments.queries import counts_for_page

        comment_counts = await counts_for_page(
            ctx.db,
            admin.model_label,
            [str(admin.get_pk(obj)) for obj in objects],
        )
    rows: list[dict[str, Any]] = []
    for obj, row_edits, row_headers in zip(objects, edits, headers, strict=True):
        pk = admin.get_pk(obj)
        history_url = (
            admin.history_path(pk)
            if has_history_ui and admin.has_history_permission(ctx, obj)
            else None
        )
        comments_url = (
            admin.comments_path(pk)
            if has_comments_ui and admin.has_comment_view_permission(ctx, obj)
            else None
        )
        rows.append(
            {
                "groups": row_headers,
                "pk": pk,
                "label": _object_label(admin, obj),
                "cells": [admin.build_cell(spec, obj) for spec in columns],
                "edits": row_edits,
                "history_url": history_url,
                "comments_url": comments_url,
                "comments_count": comment_counts.get(str(pk), 0) if comments_url else 0,
            }
        )
    pages = max(1, -(-total // query.size))
    inline_action = f"{admin.list_path()}/inline"
    if request.url.query:
        inline_action = f"{inline_action}?{request.url.query}"
    bulk_actions = admin.get_actions(ctx)
    bulk_action = f"{admin.list_path()}/actions"
    if request.url.query:
        bulk_action = f"{bulk_action}?{request.url.query}"
    tree = query.combined_filters()
    query_state = query.to_params()
    query_state.pop("f", None)
    query_state.pop("page", None)
    query_state["filter"] = json.dumps(tree.to_dict(), ensure_ascii=False)
    context = await page_context(
        request,
        query_state=query_state,
        filter_tree=tree.to_dict(),
        query_fields=[
            {
                "name": spec.name,
                "label": spec.label,
                "type": spec.type.name,
                "sortable": spec.type.sortable,
                "operators": [
                    {"name": op.name, "label": op.label} for op in spec.type.operators.values()
                ],
                "choices": _spec_choices(spec),
            }
            for spec in specs.values()
            if spec.column is not None and not spec.secret
        ],
        sort_terms=[{"field": term.field, "descending": term.descending} for term in query.sort],
        group_terms=[{"field": term.field, "descending": term.descending} for term in query.group],
        saved_views=(
            await ctx.db.scalars(view_scope(ctx).order_by(SavedView.name, SavedView.id))
        ).all(),
        selected_view=selected_view,
        admin=admin,
        columns=columns,
        rows=rows,
        q=query.q,
        page=query.page,
        pages=pages,
        size=query.size,
        start_index=offset + 1,
        total=total,
        total_capped=total > COUNT_CAP,
        displayed_total=min(total, COUNT_CAP),
        searchable=bool(admin.get_search_specs()),
        can_add=admin.has_add_permission(ctx),
        sort_state=lambda field: _sort_state(request, field, query),
        sort_href=lambda field: _sort_href(request, field, query),
        delete_href=lambda pk: admin.delete_path(pk),
        inline_columns=can_inline,
        inline_action=inline_action,
        inline_banner=banner,
        bulk_actions=bulk_actions,
        bulk_action=bulk_action,
    )
    return render(request, "admin/model/list.html", context)


def _inline_edits(
    admin: ModelAdmin,
    columns: Sequence[FieldSpec],
    objects: Sequence[Any],
    values: Mapping[tuple[str, str], str] | None,
    errors: Mapping[tuple[str, str], str] | None,
) -> list[list[dict[str, Any] | None]]:
    inline_names = {spec.name for spec in admin.get_inline_specs()}
    edits: list[list[dict[str, Any] | None]] = []
    for obj in objects:
        pk_key = str(admin.get_pk(obj))
        row_edits: list[dict[str, Any] | None] = []
        for spec in columns:
            if spec.name not in inline_names:
                row_edits.append(None)
                continue
            attr = spec.options.get("value_attr", spec.name)
            current = getattr(obj, attr, None)
            choices: list[tuple[str, str]] = []
            if spec.type.name == "checkbox":
                kind, step = "select", ""
                original = "true" if current else "false"
                choices = [("true", "Yes"), ("false", "No")]
            elif spec.type.name == "single_select":
                kind, step = "select", ""
                original = spec.type.to_input(current, spec)
                choices = _spec_choices(spec)
            elif spec.type.name == "integer":
                kind, step = "number", "1"
                original = spec.type.to_input(current, spec)
            elif spec.type.name == "decimal":
                kind, step = "number", "any"
                original = spec.type.to_input(current, spec)
            else:
                kind, step = "text", ""
                original = spec.type.to_input(current, spec)
            value = original
            if values is not None and (pk_key, spec.name) in values:
                value = values[(pk_key, spec.name)]
            row_edits.append(
                {
                    "name": spec.name,
                    "kind": kind,
                    "step": step,
                    "value": value,
                    "original": original,
                    "choices": choices,
                    "modified": value != original,
                    "error": (errors or {}).get((pk_key, spec.name), ""),
                }
            )
        edits.append(row_edits)
    return edits


def _sort_terms(request: Request, query: QuerySpec | None = None) -> list[str]:
    if query is not None:
        return [f"{'-' if term.descending else ''}{term.field}" for term in query.sort]
    terms: list[str] = []
    for raw in request.query_params.getlist("sort"):
        terms.extend(part.strip() for part in raw.split(",") if part.strip())
    return terms


def _sort_state(request: Request, field: str, query: QuerySpec | None = None) -> str:
    for term in _sort_terms(request, query):
        if term == field:
            return "asc"
        if term == f"-{field}":
            return "desc"
    return ""


def _sort_href(request: Request, field: str, query: QuerySpec | None = None) -> str:
    terms = (
        _sort_terms(request, query)
        if "sort" in request.query_params or "view" in request.query_params
        else []
    )
    current = _sort_state(request, field, query)
    updated = [term for term in terms if term.lstrip("-") != field]
    updated = updated[:4]
    if current == "asc":
        updated.append(f"-{field}")
    elif current == "desc":
        updated.append(field)
    else:
        updated.append(f"-{field}")
    pairs = [
        (key, value)
        for key, value in request.query_params.multi_items()
        if key not in {"sort", "page"}
    ]
    if updated:
        pairs.append(("sort", ",".join(updated)))
    return f"{request.url.path}?{urlencode(pairs)}"


# -- forms ------------------------------------------------------------------


async def _render_form(
    ctx: AdminContext,
    *,
    target: Any = None,
    readonly: bool = False,
    values: Mapping[str, str] | None = None,
    errors: Mapping[str, Sequence[str]] | None = None,
) -> HTMLResponse:
    admin = _require_admin(ctx)
    request = ctx.request
    is_add = target is None
    specs = [spec for spec in admin.get_form_specs() if not spec.readonly]
    if values is None:
        values = _initial_values(ctx, specs)
    history: Mapping[str, Any] | None = None
    if (
        target is not None
        and timeline_available(admin)
        and admin.has_history_permission(ctx, target)
    ):
        pk = admin.get_pk(target)
        history = await build_card(
            ctx.db,
            admin.model,
            pk,
            url=admin.history_path(pk),
            mask=diff_mask(admin.model, admin=admin, user=ctx.user),
            revert=_revert_links(ctx, admin, pk),
        )
    comments: Mapping[str, Any] | None = None
    if (
        target is not None
        and admin.comments_available()
        and admin.has_comment_view_permission(ctx, target)
    ):
        from obox_builder.contrib.comments.policy import CommentPolicy
        from obox_builder.contrib.comments.queries import latest_threads
        from obox_builder.contrib.comments.registry import get_options
        from obox_builder.contrib.comments.targets import CommentTarget

        pk = admin.get_pk(target)
        comment_target = CommentTarget(
            admin.model_label, str(pk), _object_label(admin, target)[:255]
        )
        policy = CommentPolicy(
            user=ctx.user,
            view_permission=admin.permission_name("view"),
            change_permission=admin.permission_name("change"),
        )
        card = await latest_threads(ctx.db, comment_target, policy, get_options(admin.model))
        comments = {
            "count": card.count,
            "threads": card.threads,
            "url": admin.comments_path(pk),
        }
    context = await page_context(
        request,
        admin=admin,
        specs=specs,
        values=dict(values or {}),
        errors=dict(errors or {}),
        choices=await _choice_map(ctx, specs),
        readonly=readonly,
        is_add=is_add,
        action=admin.add_path() if is_add else admin.change_path(admin.get_pk(target)),
        title=f"New {admin.name.lower()}" if is_add else f"Edit {admin.name.lower()}",
        subtitle="" if is_add else _object_label(admin, target),
        history=history,
        comments=comments,
    )
    return render(request, "admin/model/form.html", context)


async def _create(ctx: AdminContext) -> HTMLResponse | RedirectResponse:
    admin = _require_admin(ctx)
    request = ctx.request
    data = await _submitted_data(request)
    await ensure_csrf(request, data.get("csrf_token"))
    specs = admin.get_form_specs()
    schema = admin.get_form_schema("add")
    bound = BoundForm.bind(specs, schema, data, base=ctx.record_context())
    if not bound.is_valid:
        return await _render_form(ctx, values=_values_from_data(data, specs), errors=bound.errors)
    obj = admin.model()
    bound.apply_to(obj)
    try:
        async with ctx.db.begin_nested():
            ctx.db.add(obj)
            await ctx.db.flush()
    except IntegrityError as exc:
        return await _render_form(
            ctx,
            values=_values_from_data(data, specs),
            errors={"__all__": [_integrity_message(exc)]},
        )
    await flash(request, f"{admin.name} {_object_label(admin, obj)!r} was created.")
    target = _redirect_after_save(admin, str(data.get("_action", "save")), obj)
    return RedirectResponse(target, status_code=303)


async def _update(ctx: AdminContext, target: Any) -> HTMLResponse | RedirectResponse:
    admin = _require_admin(ctx)
    request = ctx.request
    data = await _submitted_data(request)
    await ensure_csrf(request, data.get("csrf_token"))
    specs = admin.get_form_specs()
    schema = admin.get_form_schema("change")
    ctx.obj = target
    bound = BoundForm.bind(specs, schema, data, base=ctx.record_context())
    if not bound.is_valid:
        return await _render_form(
            ctx,
            target=target,
            values=_values_from_data(data, specs),
            errors=bound.errors,
        )
    bound.apply_to(target)
    try:
        async with ctx.db.begin_nested():
            await ctx.db.flush()
    except IntegrityError as exc:
        await ctx.db.refresh(target)
        return await _render_form(
            ctx,
            target=target,
            values=_values_from_data(data, specs),
            errors={"__all__": [_integrity_message(exc)]},
        )
    await flash(request, f"{admin.name} {_object_label(admin, target)!r} was updated.")
    destination = _redirect_after_save(admin, str(data.get("_action", "save")), target)
    return RedirectResponse(destination, status_code=303)


async def _inline(ctx: AdminContext) -> HTMLResponse | RedirectResponse:
    admin = _require_admin(ctx)
    request = ctx.request
    specs = admin.get_inline_specs()
    if not specs:
        return _bad_request(ctx, f"Inline editing is not enabled for {admin.name}.")
    data = await _submitted_data(request)
    await ensure_csrf(request, data.get("csrf_token"))
    names = {spec.name for spec in specs}
    posted: dict[str, dict[str, str]] = {}
    for key, value in data.items():
        if not key.startswith("edit:"):
            continue
        _, _, remainder = key.partition(":")
        pk_key, _, field = remainder.rpartition(":")
        if field in names and pk_key:
            posted.setdefault(pk_key, {})[field] = value
    destination = admin.list_path()
    if request.url.query:
        destination = f"{destination}?{request.url.query}"
    if not posted:
        await flash(request, "No changes to save.")
        return RedirectResponse(destination, status_code=303)
    try:
        pks = [admin.parse_pk(pk_key) for pk_key in posted]
    except LookupError:
        return _bad_request(ctx, "Invalid row id in the inline edit submission.")
    stmt = admin.get_queryset(ctx).where(admin.pk_column().in_(pks))
    objects = (await ctx.db.execute(stmt)).scalars().unique().all()
    schema = admin.get_inline_schema()
    values: dict[tuple[str, str], str] = {
        (pk_key, name): raw for pk_key, row in posted.items() for name, raw in row.items()
    }
    errors: dict[tuple[str, str], str] = {}
    banner = ""
    pending: list[tuple[Any, BoundForm]] = []
    for obj in objects:
        pk_key = str(admin.get_pk(obj))
        row_data = posted.get(pk_key)
        if row_data is None:
            continue
        ctx.obj = obj
        bound = BoundForm.bind(specs, schema, row_data, base=ctx.record_context())
        if bound.is_valid:
            pending.append((obj, bound))
            continue
        for name, messages in bound.errors.items():
            if name == "__all__":
                banner = messages[-1]
            else:
                errors[(pk_key, name)] = messages[-1]
    if errors or banner:
        if not banner:
            banner = "Nothing was saved — fix the highlighted values and try again."
        return await _list(ctx, inline_values=values, inline_errors=errors, banner=banner)
    changed = 0
    for obj, bound in pending:
        if any(
            spec.type.to_input(getattr(obj, spec.name, None), spec)
            != spec.type.to_input(bound.cleaned.get(spec.name), spec)
            for spec in specs
        ):
            bound.apply_to(obj)
            changed += 1
    try:
        async with ctx.db.begin_nested():
            await ctx.db.flush()
    except IntegrityError as exc:
        for obj, _bound in pending:
            await ctx.db.refresh(obj)
        return await _list(ctx, inline_values=values, banner=_integrity_message(exc))
    if changed:
        noun = admin.name.lower() if changed == 1 else admin.plural_name.lower()
        await flash(request, f"{changed} {noun} updated.")
    else:
        await flash(request, "No changes to save.")
    return RedirectResponse(destination, status_code=303)


async def _actions(ctx: AdminContext) -> Response:
    admin = _require_admin(ctx)
    request = ctx.request
    form = await request.form()
    token = form.get("csrf_token")
    await ensure_csrf(request, token if isinstance(token, str) else None)
    specs = {spec.name: spec for spec in admin.get_actions(ctx)}
    if not specs:
        return _bad_request(ctx, f"Bulk actions are not enabled for {admin.name}.")
    destination = admin.list_path()
    if request.url.query:
        destination = f"{destination}?{request.url.query}"
    raw_action = form.get("action")
    spec = specs.get(raw_action if isinstance(raw_action, str) else "")
    if spec is None:
        await flash(request, "No action selected.", "warning")
        return RedirectResponse(destination, status_code=303)
    select_across = form.get("select_across") == "1"
    selected = [value for value in form.getlist("_selected_action") if isinstance(value, str)]
    if not select_across and not selected:
        await flash(
            request,
            "Items must be selected in order to perform actions on them. Nothing was changed.",
            "warning",
        )
        return RedirectResponse(destination, status_code=303)
    field_specs = {field.name: field for field in admin.get_field_specs()}
    try:
        query, _selected = await load_query(ctx)
        stmt = query.apply(
            admin.get_queryset(ctx),
            field_specs,
            search_specs=admin.get_search_specs(),
            text_collation=settings.ADMIN_TEXT_COLLATION,
        )
    except QuerySpecError as exc:
        return _bad_request(ctx, str(exc))
    except LookupError as exc:
        return _not_found(ctx, str(exc))
    if not select_across:
        try:
            pks = [admin.parse_pk(value) for value in selected]
        except LookupError:
            return _bad_request(ctx, "Invalid row id in the action submission.")
        stmt = stmt.where(admin.pk_column().in_(pks))
    result = await spec.func(ctx, stmt)
    if isinstance(result, Response):
        return result
    return RedirectResponse(destination, status_code=303)


# -- history ----------------------------------------------------------------


async def _history(ctx: AdminContext, target: Any, pk: Any) -> HTMLResponse:
    admin = _require_admin(ctx)
    request = ctx.request
    deleted = target is None
    context = await page_context(
        request,
        **await timeline_context(
            ctx.db,
            admin.model,
            pk,
            before=before_cursor(request),
            heading=f"{admin.name} history",
            subtitle=f"#{pk}" if deleted else _object_label(admin, target),
            back_url=admin.list_path() if deleted else admin.change_path(pk),
            timeline_url=request.url.path,
            mask=diff_mask(admin.model, admin=admin, user=ctx.user),
            revert=_revert_links(ctx, admin, pk),
            deleted=deleted,
        ),
    )
    return render(request, "admin/history.html", context)


def _revert_links(ctx: AdminContext, admin: ModelAdmin, pk: Any) -> RevertLinks | None:
    """Revert wiring for a timeline, or ``None`` when the user cannot restore."""
    if not ctx.user.has_perm(HISTORY_RESTORE_PERMISSION):
        return None

    def can_revert(entry: RowChangeEntry) -> bool:
        action = OP_PERMISSION.get(entry.op, "change")
        return bool(getattr(admin, f"has_{action}_permission")(ctx))

    return RevertLinks(
        base_url=admin.history_path(pk),
        specs={spec.name: spec for spec in admin.get_form_specs()},
        can_revert=can_revert,
    )


# -- delete -----------------------------------------------------------------


async def _delete(ctx: AdminContext, target: Any) -> HTMLResponse | RedirectResponse:
    admin = _require_admin(ctx)
    request = ctx.request
    form = await request.form()
    token = form.get("csrf_token")
    await ensure_csrf(request, token if isinstance(token, str) else None)
    label = _object_label(admin, target)
    try:
        async with ctx.db.begin_nested():
            await ctx.db.delete(target)
            await ctx.db.flush()
    except IntegrityError as exc:
        await ctx.db.refresh(target)
        context = await page_context(
            request, admin=admin, target=target, error=_integrity_message(exc)
        )
        return render(request, "admin/model/delete.html", context, status_code=409)
    await flash(request, f"{admin.name} {label!r} was deleted.")
    return RedirectResponse(admin.list_path(), status_code=303)


# -- helpers ----------------------------------------------------------------


def _require_admin(ctx: AdminContext) -> ModelAdmin:
    if ctx.admin is None:  # pragma: no cover - model routes always set it
        raise RuntimeError("AdminContext without a ModelAdmin on a model route.")
    return ctx.admin


def _redirect_after_save(admin: ModelAdmin, action: str, target: Any) -> str:
    if action == "add_another":
        return admin.add_path()
    if action == "continue":
        return admin.change_path(admin.get_pk(target))
    return admin.list_path()


async def _submitted_data(request: Request) -> dict[str, Any]:
    form = await request.form()
    return {key: value for key, value in form.multi_items() if isinstance(value, str)}


def _values_from_data(data: Mapping[str, Any], specs: Sequence[FieldSpec]) -> dict[str, str]:
    return {
        spec.name: spec.type.to_input(spec.type.from_input(data.get(spec.name), spec), spec)
        for spec in specs
    }


def _initial_values(ctx: AdminContext, specs: Sequence[FieldSpec]) -> dict[str, str]:
    record = ctx.record_context()
    values: dict[str, str] = {}
    for spec in specs:
        initial = spec.initial(record) if callable(spec.initial) else spec.initial
        values[spec.name] = spec.type.to_input(initial, spec)
    return values


async def _choice_map(
    ctx: AdminContext, specs: Sequence[FieldSpec]
) -> dict[str, list[tuple[str, str]]]:
    choices: dict[str, list[tuple[str, str]]] = {}
    for spec in specs:
        if spec.type.name == "single_select":
            choices[spec.name] = _spec_choices(spec)
        elif spec.type.name == "link":
            choices[spec.name] = await _link_choices(ctx, spec)
    return choices


async def _link_choices(ctx: AdminContext, spec: FieldSpec) -> list[tuple[str, str]]:
    target = spec.options.get("target")
    if target is None:
        return []
    pk_column = sa.inspect(target).primary_key[0]
    stmt = sa.select(target).order_by(pk_column).limit(LINK_CHOICE_LIMIT)
    rows = (await ctx.db.execute(stmt)).scalars().all()
    return [(str(getattr(row, pk_column.key)), _object_label_for(target, row)) for row in rows]


async def _get_object(ctx: AdminContext, pk: str) -> Any:
    admin = _require_admin(ctx)
    value = admin.parse_pk(pk)
    obj = await ctx.db.get(admin.model, value)
    if obj is None:
        raise LookupError(f"{admin.name} with id {pk!r} does not exist.")
    return obj


def _object_label(admin: ModelAdmin, obj: Any) -> str:
    return _object_label_for(admin.model, obj) if obj is not None else ""


def _object_label_for(model: type[Any], obj: Any) -> str:
    pk_key = sa.inspect(model).primary_key[0].key
    text = str(obj)
    if text.startswith("<"):
        return f"#{getattr(obj, pk_key, '?')}"
    return text


def _integrity_message(exc: IntegrityError) -> str:
    return f"Could not save: the database rejected the change ({exc.orig})."


def _bad_request(ctx: AdminContext, message: str) -> HTMLResponse:
    context = {"site_name": "obox admin", "message": message}
    return render(ctx.request, "admin/errors/400.html", context, status_code=400)


def _not_found(ctx: AdminContext, message: str) -> HTMLResponse:
    context = {"site_name": "obox admin", "message": message}
    return render(ctx.request, "admin/errors/404.html", context, status_code=404)


def _spec_choices(spec: FieldSpec) -> list[tuple[str, str]]:
    getter = getattr(spec.type, "choices", None)
    if callable(getter):
        result = getter(spec)
        if isinstance(result, (list, tuple)):
            return [(str(value), str(label)) for value, label in result]
    return []
