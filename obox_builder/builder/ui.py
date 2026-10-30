"""Escaped HTML CRUD using the same service and policy as REST."""

from __future__ import annotations

import enum
import json
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from urllib.parse import quote, urlencode

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, PackageLoader, select_autoescape

from obox_builder.builder.api import query_options
from obox_builder.builder.http import read_form
from obox_builder.builder.service import invoke
from obox_builder.context import Context
from obox_builder.errors import BuilderValidationError

templates = Environment(
    loader=PackageLoader("obox_builder.builder", "templates"),
    autoescape=select_autoescape(("html",)),
)


def render(template: str, *, status: int = 200, **context: Any) -> HTMLResponse:
    return HTMLResponse(templates.get_template(template).render(**context), status_code=status)


async def actions(builder: Any, ctx: Context, obj: Any = None) -> tuple[str, ...]:
    requested = builder.view.get_actions(ctx, instance=obj)
    allowed = []
    for action in builder.view.actions:
        if action not in requested or (action in {"create", "update"} and not builder.form.enabled):
            continue
        if await invoke(builder.policy.allow, action, ctx, obj=obj):
            allowed.append(action)
    return tuple(allowed)


async def reference(app: Any, spec: Any, value: Any, ctx: Context) -> str | None:
    if value is None:
        return None
    for fk in spec.column.foreign_keys:
        target = app._for_table(fk.column.table)
        if target is None or not target.view.enabled:
            continue
        if not await invoke(target.policy.allow, "retrieve", ctx):
            continue
        remote = sa.inspect(target.model).get_property_by_column(fk.column).key
        stmt = (
            app.service(target.model)
            ._statement("retrieve", ctx)
            .where(getattr(target.model, remote) == value)
        )
        obj = (await ctx.session.scalars(stmt)).unique().one_or_none()
        if obj is not None and await invoke(target.policy.allow, "retrieve", ctx, obj=obj):
            return (
                f"{app.ui_prefix}/{target.slug}/{quote(str(getattr(obj, target.pk_name)), safe='')}"
            )
    return None


async def form_fields(builder: Any, service: Any, ctx: Context, obj: Any) -> list[dict[str, Any]]:
    action = "create" if obj is None else "update"
    allowed = service.write_fields(action, ctx, obj)
    result = []
    for name in allowed:
        spec = builder.specs[name]
        column = spec.column
        kind = column.type.python_type
        options = None
        widget = "text"
        if column.foreign_keys:
            choices = []
            for fk in column.foreign_keys:
                target = builder.app._for_table(fk.column.table)
                if target is None:
                    continue
                if not await invoke(target.policy.allow, "list", ctx):
                    continue
                if not await invoke(target.policy.allow, "retrieve", ctx):
                    continue
                stmt = (
                    builder.app.service(target.model)
                    ._statement("list", ctx)
                    .where(target.policy.scope("retrieve", ctx))
                    .order_by(target.pk)
                )
                remote = sa.inspect(target.model).get_property_by_column(fk.column).key
                for row in (await ctx.session.scalars(stmt)).unique():
                    if not await invoke(target.policy.allow, "retrieve", ctx, obj=row):
                        continue
                    if remote != target.pk_name and remote not in target.readable(ctx, obj=row):
                        continue
                    visible = await builder.app.service(target.model).serialize(row, ctx=ctx)
                    label = next(
                        (str(v) for k, v in visible.items() if k != target.pk_name), "Record"
                    )
                    choices.append((str(getattr(row, remote)), label))
            if not any(builder.app._for_table(fk.column.table) for fk in column.foreign_keys):
                continue
            options, widget = choices, "select"
        elif isinstance(kind, type) and issubclass(kind, enum.Enum):
            options, widget = [(str(item.value), str(item.value)) for item in kind], "select"
        elif kind is bool:
            options, widget = [("true", "Yes"), ("false", "No")], "select"
        elif isinstance(column.type, sa.JSON):
            widget = "textarea"
        elif kind is datetime:
            widget = "text" if getattr(column.type, "timezone", False) else "datetime-local"
        elif kind is date:
            widget = "date"
        elif kind is time:
            widget = "time"
        elif kind in {int, float, Decimal}:
            widget = "number"
        config = dict(builder.form.widgets.get(name, {}))
        widget = config.get("type", widget)
        if widget not in {
            "text",
            "textarea",
            "select",
            "number",
            "date",
            "datetime-local",
            "time",
            "email",
            "url",
            "password",
        }:
            raise BuilderValidationError({name: ["Unsupported form widget."]})
        result.append(
            {
                "name": name,
                "label": builder.form.labels.get(name, name.replace("_", " ").capitalize()),
                "help": builder.form.help_texts.get(name, ""),
                "widget": widget,
                "options": options or [],
                "required": spec.required,
                "nullable": column.nullable,
                "maxlength": getattr(column.type, "length", None),
                "placeholder": config.get("placeholder", ""),
            }
        )
    return result


def input_values(builder: Any, data: dict[str, Any]) -> dict[str, str]:
    result = {}
    for name, value in data.items():
        if name not in builder.specs:
            continue
        column = builder.specs[name].column
        if value is None:
            result[name] = ""
        elif isinstance(column.type, sa.JSON):
            result[name] = json.dumps(value, ensure_ascii=False)
        elif isinstance(value, bool):
            result[name] = "true" if value else "false"
        elif isinstance(value, enum.Enum):
            result[name] = str(value.value)
        elif isinstance(value, (datetime, date, time)):
            result[name] = value.isoformat()
        else:
            result[name] = str(value)
    return result


def build_ui(app: Any, builder: Any, service: Any) -> APIRouter:
    router = APIRouter()
    path = f"{app.ui_prefix}/{builder.slug}"
    dependency = Depends(app.context_dependency("ui"), scope="function")

    def base(request: Request) -> str:
        return request.scope.get("root_path", "") + path

    async def list_records(request: Request, ctx: Context = dependency) -> HTMLResponse:
        options = query_options(request, builder, pagination=True)
        options["filters"] = {
            key: value for key, value in options["filters"].items() if value != ""
        }
        if builder.view.page_size is not None:
            options.setdefault("limit", builder.view.page_size)
        objects = await service.list(ctx=ctx, **options)
        columns = tuple(key for key in builder.view.get_columns(ctx) if key in builder.view_columns)
        columns = builder.readable(ctx, fields=columns)
        rows = []
        for obj in objects:
            visible = await service.serialize(obj, ctx=ctx, fields=columns)
            can_detail = await invoke(builder.policy.allow, "retrieve", ctx, obj=obj)
            rows.append(
                {
                    "values": visible,
                    "url": f"{base(request)}/{quote(str(getattr(obj, builder.pk_name)), safe='')}",
                    "detail": can_detail,
                }
            )
        extra = builder.view.get_context(ctx)
        limit = options.get("limit")
        offset = options.get("offset", 0)
        next_query = dict(request.query_params)
        if limit is not None:
            next_query.update(offset=str(offset + limit), limit=str(limit))
        filters = tuple(
            key
            for key in builder.view.get_filters(ctx)
            if key in builder.filter_fields and key in builder.readable(ctx)
        )
        return render(
            "list.html",
            title=extra.get("title", builder.model.__name__),
            base=base(request),
            columns=columns,
            rows=rows,
            actions=await actions(builder, ctx),
            search=any(key in builder.readable(ctx) for key in builder.search_fields),
            filters=filters,
            sort_fields=tuple(key for key in builder.sort_fields if key in builder.readable(ctx)),
            query=request.query_params,
            next_url="?" + urlencode(next_query) if limit and len(objects) == limit else None,
        )

    async def detail(request: Request, id: str, ctx: Context = dependency) -> HTMLResponse:
        obj = await service.get(id, ctx=ctx)
        visible = await service.serialize(obj, ctx=ctx, fields=builder.view_details)
        links = {
            key: await reference(app, builder.specs[key], value, ctx)
            for key, value in visible.items()
        }
        links = {
            key: request.scope.get("root_path", "") + value for key, value in links.items() if value
        }
        extra = builder.view.get_context(ctx, instance=obj)
        collections = []
        allowed = builder.policy.read_fields(ctx, obj=obj)
        for name in builder.view_related:
            if allowed is not None and name not in allowed:
                continue
            relation = sa.inspect(builder.model).relationships[name]
            target = app.get_builder(relation.mapper.class_)
            if not await invoke(target.policy.allow, "list", ctx):
                continue
            records = await service.related(id, name, ctx=ctx)
            target_service = app.service(target.model)
            columns = target.readable(ctx, fields=target.view_columns)
            collections.append(
                {
                    "name": name,
                    "columns": columns,
                    "rows": [
                        await target_service.serialize(row, ctx=ctx, fields=columns)
                        for row in records
                    ],
                }
            )
        return render(
            "detail.html",
            title=extra.get("title", builder.model.__name__),
            base=base(request),
            values=visible,
            collections=collections,
            links=links,
            actions=await actions(builder, ctx, obj),
            url=f"{base(request)}/{quote(str(getattr(obj, builder.pk_name)), safe='')}",
            csrf=app.csrf.token(request, ctx.principal),
        )

    async def form_page(request: Request, ctx: Context, id: str | None = None) -> Any:
        obj = await service._get(id, ctx, "update") if id is not None else None
        action = "create" if obj is None else "update"
        await service.authorize(action, ctx, obj)
        controls = await form_fields(builder, service, ctx, obj)
        names = tuple(field["name"] for field in controls)
        if obj is None:
            values = {
                key: builder.specs[key].column.default.arg
                for key in names
                if builder.specs[key].column.default is not None
                and builder.specs[key].column.default.is_scalar
            }
            values.update(builder.form.get_initial(ctx))
        else:
            values = {key: getattr(obj, key) for key in names}
        errors = {}
        status = 200
        if request.method == "POST":
            form = await read_form(request)
            app.csrf.validate(request, ctx.principal, str(form.get("csrf_token", "")))
            if any(len(form.getlist(key)) != 1 for key in form):
                raise BuilderValidationError(
                    {"__all__": ["Duplicate form fields are not permitted."]}
                )
            values = {key: str(value) for key, value in form.items() if key != "csrf_token"}
            try:
                converted: dict[str, Any] = dict(values)
                for key, value in values.items():
                    if key not in builder.specs:
                        continue
                    column = builder.specs[key].column
                    if value == "" and column.nullable:
                        converted[key] = None
                    elif (
                        value == ""
                        and obj is None
                        and column.type.python_type is not str
                        and (column.default is not None or column.server_default is not None)
                    ):
                        converted.pop(key)
                    elif isinstance(column.type, sa.JSON):
                        try:
                            converted[key] = json.loads(value)
                        except (TypeError, ValueError) as exc:
                            raise BuilderValidationError({key: ["Enter valid JSON."]}) from exc
                if obj is None:
                    await service.create(converted, ctx=ctx)
                else:
                    await service.update(id, converted, ctx=ctx)
                return RedirectResponse(
                    base(request) if builder.view.enabled else request.url.path, status_code=303
                )
            except BuilderValidationError as exc:
                if ctx._failed:
                    raise
                errors, status = exc.errors, 422
        extra = builder.form.get_context(ctx, instance=obj, action=action)
        return render(
            "form.html",
            status=status,
            title=extra.get("title", builder.model.__name__),
            base=base(request),
            fields=controls,
            values=values if request.method == "POST" else input_values(builder, values),
            errors=errors,
            csrf=app.csrf.token(request, ctx.principal),
            action=action,
        )

    async def create_form(request: Request, ctx: Context = dependency) -> Any:
        return await form_page(request, ctx)

    async def update_form(request: Request, id: str, ctx: Context = dependency) -> Any:
        return await form_page(request, ctx, id)

    async def delete(request: Request, id: str, ctx: Context = dependency) -> RedirectResponse:
        form = await read_form(request)
        app.csrf.validate(request, ctx.principal, str(form.get("csrf_token", "")))
        await service.delete(id, ctx=ctx)
        return RedirectResponse(base(request), status_code=303)

    for endpoint, suffix, methods, enabled, name in (
        (list_records, "", ["GET"], builder.view.enabled, "list"),
        (
            create_form,
            "/new",
            ["GET", "POST"],
            builder.form.enabled,
            "create",
        ),
        (detail, "/{id}", ["GET"], builder.view.enabled, "detail"),
        (
            update_form,
            "/{id}/edit",
            ["GET", "POST"],
            builder.form.enabled,
            "update",
        ),
        (
            delete,
            "/{id}/delete",
            ["POST"],
            builder.view.enabled,
            "delete",
        ),
    ):
        if enabled:
            router.add_api_route(
                path + suffix,
                endpoint,
                methods=methods,
                response_class=HTMLResponse,
                name=f"obox_ui_{builder.slug}_{name}",
                include_in_schema=False,
            )
    return router
