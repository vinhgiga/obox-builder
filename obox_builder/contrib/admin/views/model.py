"""Generic CRUD views for registered models (full-page, server-rendered).

One set of endpoints is generated per registered ModelAdmin at router build
time, so every model gets list/add/change/delete with the same behaviour and
the OpenAPI schema (M9) can enumerate them.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlencode

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from obox_builder.contrib.admin.context import AdminContext
from obox_builder.contrib.admin.options import ModelAdmin
from obox_builder.contrib.admin.site import AdminSite
from obox_builder.contrib.admin.views.common import (
    ensure_csrf,
    flash,
    page_context,
)
from obox_builder.contrib.auth.dependencies import permission_required
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

    router.add_api_route(base, list_view, methods=["GET"], name=f"{name}_list")
    router.add_api_route(f"{base}/add", add_view, methods=["GET"], name=f"{name}_add")
    router.add_api_route(f"{base}/add", add_submit, methods=["POST"], name=f"{name}_add_submit")
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


async def _list(ctx: AdminContext) -> HTMLResponse:
    admin = _require_admin(ctx)
    request = ctx.request
    specs = {spec.name: spec for spec in admin.get_field_specs()}
    try:
        query = QuerySpec.parse(request.query_params, specs, default_sort=admin.get_ordering(ctx))
    except QuerySpecError as exc:
        return _bad_request(ctx, str(exc))

    stmt = admin.get_queryset(ctx)
    try:
        stmt = query.apply(stmt, specs, search_specs=admin.get_search_specs())
    except QuerySpecError as exc:
        return _bad_request(ctx, str(exc))

    total = await ctx.db.scalar(
        sa.select(sa.func.count()).select_from(stmt.order_by(None).subquery())
    )
    total = int(total or 0)
    offset = (query.page - 1) * query.size
    result = await ctx.db.execute(stmt.limit(query.size).offset(offset))
    objects = result.scalars().unique().all()

    columns = admin.get_list_specs()
    rows = [
        {
            "pk": admin.get_pk(obj),
            "label": _object_label(admin, obj),
            "cells": [admin.build_cell(spec, obj) for spec in columns],
        }
        for obj in objects
    ]
    pages = max(1, -(-total // query.size))
    filters = _build_filters(request, admin)
    context = await page_context(
        request,
        admin=admin,
        columns=columns,
        rows=rows,
        q=query.q,
        page=query.page,
        pages=pages,
        size=query.size,
        total=total,
        total_capped=total > COUNT_CAP,
        displayed_total=min(total, COUNT_CAP),
        searchable=bool(admin.get_search_specs()),
        can_add=admin.has_add_permission(ctx),
        filters=filters,
        sort_state=lambda field: _sort_state(request, field),
        sort_href=lambda field: _sort_href(request, field),
        delete_href=lambda pk: admin.delete_path(pk),
    )
    return render(request, "admin/model/list.html", context)


def _sort_terms(request: Request) -> list[str]:
    terms: list[str] = []
    for raw in request.query_params.getlist("sort"):
        terms.extend(part.strip() for part in raw.split(",") if part.strip())
    return terms


def _sort_state(request: Request, field: str) -> str:
    for term in _sort_terms(request):
        if term == field:
            return "asc"
        if term == f"-{field}":
            return "desc"
    return ""


def _sort_href(request: Request, field: str) -> str:
    terms = _sort_terms(request)
    current = _sort_state(request, field)
    updated = [term for term in terms if term.lstrip("-") != field]
    if current == "asc":
        updated.append(f"-{field}")
    elif current == "desc":
        updated.append(field)
    else:
        updated.append(f"-{field}")
    pairs = [(key, value) for key, value in request.query_params.multi_items() if key != "sort"]
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


def _build_filters(request: Request, admin: ModelAdmin) -> list[dict[str, Any]]:
    filters: list[dict[str, Any]] = []
    for spec in admin.get_filter_specs():
        if spec.type.name == "single_select":
            options = _spec_choices(spec)
        elif spec.type.name == "checkbox":
            options = [("true", "Yes"), ("false", "No")]
        else:
            continue
        current = ""
        for term in request.query_params.getlist("f"):
            field_name, _, remainder = str(term).partition(":")
            if field_name == spec.name and ":" in remainder:
                _op, _, value = remainder.partition(":")
                current = value
        filters.append({"spec": spec, "options": options, "current": current})
    return filters
