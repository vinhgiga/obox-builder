"""Escaped HTML CRUD using the same service and policy as REST."""

from __future__ import annotations

import enum
import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from urllib.parse import quote, urlencode

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, Environment, FileSystemLoader, PackageLoader, select_autoescape

from obox_builder.builder.api import query_options
from obox_builder.builder.http import read_form
from obox_builder.builder.service import invoke
from obox_builder.conf import Settings
from obox_builder.context import Context
from obox_builder.errors import BuilderValidationError, PermissionDenied


def create_templates(settings: Settings) -> Jinja2Templates:
    if settings.templates is not None:
        return settings.templates
    return Jinja2Templates(
        env=Environment(
            loader=ChoiceLoader(
                [
                    FileSystemLoader(settings.template_dirs),
                    PackageLoader("obox_builder.builder", "templates"),
                ]
            ),
            autoescape=select_autoescape(("html",)),
        )
    )


async def allowed_operations(builder: Any, ctx: Context, obj: Any = None) -> tuple[str, ...]:
    requested = builder.view.get_operations(ctx, instance=obj)
    allowed = []
    for operation in builder.view.operations:
        if operation not in requested or (
            operation in {"create", "update"} and not builder.form.enabled
        ):
            continue
        if await invoke(builder.policy.allow, operation, ctx, obj=obj):
            allowed.append(operation)
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
            record_id = quote(str(getattr(obj, target.pk_name)), safe="")
            return f"{app.settings.ui_prefix}/{target.slug}/{record_id}"
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


def fieldset_groups(builder: Any, controls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fieldsets = builder.form.fieldsets
    if not fieldsets:
        return [{"title": None, "description": "", "fields": controls}]
    visible = {control["name"]: control for control in controls}
    groups = []
    for title, options in fieldsets:
        fields = [visible[name] for name in options["fields"] if name in visible]
        if fields:
            groups.append(
                {
                    "title": title,
                    "description": options.get("description", ""),
                    "fields": fields,
                }
            )
    return groups


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


def initial_values(builder: Any, ctx: Context, names: Sequence[str]) -> dict[str, Any]:
    values = {
        key: builder.specs[key].column.default.arg
        for key in names
        if builder.specs[key].column.default is not None
        and builder.specs[key].column.default.is_scalar
    }
    values.update(builder.form.get_initial(ctx))
    return values


def convert_values(builder: Any, data: dict[str, str], obj: Any) -> dict[str, Any]:
    converted: dict[str, Any] = dict(data)
    for key, value in data.items():
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
    return converted


async def inline_fields(
    builder: Any, service: Any, ctx: Context, obj: Any, fk_name: str
) -> list[dict[str, Any]]:
    controls = await form_fields(builder, service, ctx, obj)
    return [control for control in controls if control["name"] != fk_name]


def build_ui(app: Any, builder: Any, service: Any) -> APIRouter:
    router = APIRouter()
    path = f"{app.settings.ui_prefix}/{builder.slug}"
    dependency = Depends(app.context_dependency("ui"), scope="function")

    def base(request: Request) -> str:
        return request.scope.get("root_path", "") + path

    async def page(
        template: str,
        request: Request,
        ctx: Context,
        *,
        extra: Mapping[str, Any],
        status: int = 200,
        **context: Any,
    ) -> HTMLResponse:
        navigation = []
        for registration in app._registrations.values():
            target = registration.builder
            if target.view.enabled and await invoke(target.policy.allow, "list", ctx):
                navigation.append(
                    {
                        "label": target.model.__name__,
                        "url": request.url_for(f"obox_ui_{target.slug}_list"),
                        "active": target is builder,
                    }
                )
        return app.templates.TemplateResponse(
            request=request,
            name=template,
            context={
                **extra,
                **context,
                "request": request,
                "navigation": navigation,
                "site_title": app.title,
                "base_template": app.settings.base_template,
                "principal": ctx.principal,
                "auth": getattr(app.state, "obox_auth", None),
                "csrf": app.csrf.token(request, ctx.principal),
            },
            status_code=status,
        )

    async def inline_rows(
        ctx: Context, obj: Any, *, submitted: Mapping[str, str] | None = None
    ) -> list[dict[str, Any]]:
        groups: list[dict[str, Any]] = []
        for index, inline in enumerate(builder.inline_instances):
            child = app.get_builder(inline.model)
            child_service = app.service(inline.model)
            if not child.form.enabled or not await invoke(child.policy.allow, "list", ctx):
                continue
            allowed = builder.policy.read_fields(ctx, obj=obj)
            if allowed is not None and inline.relation_name not in allowed:
                continue
            records: dict[str, Any] = {}
            if obj is not None:
                stmt = (
                    child_service._statement("list", ctx)
                    .where(getattr(inline.model, inline.fk_name) == getattr(obj, builder.pk_name))
                    .order_by(child.pk)
                )
                for record in (await ctx.session.scalars(stmt)).unique():
                    if await invoke(child.policy.allow, "update", ctx, obj=record):
                        records[str(getattr(record, child.pk_name))] = record
            can_create = bool(await invoke(child.policy.allow, "create", ctx))
            prototype: dict[str, Any] | None = None
            if can_create:
                controls = await inline_fields(child, child_service, ctx, None, inline.fk_name)
                initial = input_values(
                    child,
                    initial_values(child, ctx, tuple(field["name"] for field in controls)),
                )
                prototype = {
                    "prefix": f"inline:{index}:__index__:",
                    "fields": controls,
                    "values": initial,
                }
            rows: list[dict[str, Any]] = []

            if submitted is None:
                for identity, record in records.items():
                    controls = await inline_fields(
                        child, child_service, ctx, record, inline.fk_name
                    )
                    rows.append(
                        {
                            "prefix": f"inline:{index}:{len(rows)}:",
                            "id": identity,
                            "fields": controls,
                            "values": input_values(
                                child,
                                {
                                    field["name"]: getattr(record, field["name"])
                                    for field in controls
                                },
                            ),
                            "errors": {},
                            "delete": False,
                            "can_delete": bool(
                                await invoke(child.policy.allow, "delete", ctx, obj=record)
                            ),
                        }
                    )
                if prototype:
                    for _ in range(inline.extra):
                        rows.append(
                            {
                                "prefix": f"inline:{index}:{len(rows)}:",
                                "id": "",
                                "fields": prototype["fields"],
                                "values": dict(prototype["values"]),
                                "errors": {},
                                "delete": False,
                                "can_delete": False,
                            }
                        )
            else:
                try:
                    count = int(submitted.get(f"inline:{index}:_total", "0"))
                except (TypeError, ValueError) as exc:
                    raise BuilderValidationError({"__all__": ["Invalid inline count."]}) from exc
                if not 0 <= count <= 1000:
                    raise BuilderValidationError({"__all__": ["Invalid inline count."]})
                for position in range(count):
                    prefix = f"inline:{index}:{position}:"
                    raw = {
                        key[len(prefix) :]: value
                        for key, value in submitted.items()
                        if key.startswith(prefix)
                    }
                    identity = raw.pop("_pk", "")
                    delete = raw.pop("_delete", "") == "1"
                    record = records.get(identity)
                    if identity and record is None:
                        raise PermissionDenied("An inline record is unavailable.")
                    controls = await inline_fields(
                        child, child_service, ctx, record, inline.fk_name
                    )
                    names = {field["name"] for field in controls}
                    initial = input_values(child, initial_values(child, ctx, tuple(names)))
                    blank = (
                        record is None
                        and not delete
                        and set(raw) <= names
                        and all(raw.get(name, "") == initial.get(name, "") for name in names)
                    )
                    row = {
                        "prefix": prefix,
                        "id": identity,
                        "fields": controls,
                        "values": initial if blank else {name: raw.get(name, "") for name in names},
                        "errors": {},
                        "delete": delete,
                        "can_delete": bool(
                            record is not None
                            and await invoke(child.policy.allow, "delete", ctx, obj=record)
                        ),
                        "_data": dict(raw),
                        "_record": record,
                        "_blank": blank,
                    }
                    if delete:
                        if record is None:
                            raise PermissionDenied("An inline record is unavailable.")
                        await child_service.authorize("delete", ctx, record)
                    elif not blank:
                        if record is None:
                            await child_service.authorize("create", ctx)
                        data = dict(raw)
                        try:
                            data = convert_values(child, data, record)
                            await child_service.validate_inline(
                                data,
                                action="update" if record is not None else "create",
                                relation=inline.fk_name,
                                ctx=ctx,
                                obj=record,
                            )
                        except BuilderValidationError as exc:
                            row["errors"] = exc.errors
                        row["_data"] = data
                    rows.append(row)
            if rows or can_create or submitted is not None:
                headers = prototype["fields"] if prototype else (rows[0]["fields"] if rows else [])
                groups.append(
                    {
                        "index": index,
                        "label": inline.label,
                        "name": inline.name,
                        "headers": headers,
                        "prototype": prototype,
                        "rows": rows,
                    }
                )
        if submitted is not None:
            known = {f"inline:{group['index']}:_total" for group in groups}
            for group in groups:
                for row in group["rows"]:
                    known.add(f"{row['prefix']}_pk")
                    known.add(f"{row['prefix']}_delete")
                    known.update(f"{row['prefix']}{key}" for key in row["_data"])
            unknown = sorted(
                key for key in submitted if key.startswith("inline:") and key not in known
            )
            if unknown:
                raise BuilderValidationError({"__all__": ["Unknown inline field."]})
        return groups

    async def list_records(request: Request, ctx: Context = dependency) -> HTMLResponse:
        options = query_options(request, builder, pagination=True)
        options["filters"] = {
            key: value for key, value in options["filters"].items() if value != ""
        }
        if builder.view.page_size is not None:
            options.setdefault("limit", builder.view.page_size)
        objects = await service.list(ctx=ctx, **options)
        list_display = tuple(
            key for key in builder.view.get_list_display(ctx) if key in builder.view_list_display
        )
        list_display = builder.readable(ctx, fields=list_display)
        rows = []
        for obj in objects:
            visible = await service.serialize(obj, ctx=ctx, fields=list_display)
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
            for key in builder.view.get_list_filter(ctx)
            if key in builder.filter_fields and key in builder.readable(ctx)
        )
        return await page(
            builder.view.list_template,
            request,
            ctx,
            extra=extra,
            title=extra.get("title", builder.model.__name__),
            base=base(request),
            list_display=list_display,
            rows=rows,
            operations=await allowed_operations(builder, ctx),
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
            list_display = target.readable(ctx, fields=target.view_list_display)
            collections.append(
                {
                    "name": name,
                    "list_display": list_display,
                    "rows": [
                        await target_service.serialize(row, ctx=ctx, fields=list_display)
                        for row in records
                    ],
                }
            )
        return await page(
            builder.view.detail_template,
            request,
            ctx,
            extra=extra,
            title=extra.get("title", builder.model.__name__),
            base=base(request),
            values=visible,
            collections=collections,
            links=links,
            operations=await allowed_operations(builder, ctx, obj),
            url=f"{base(request)}/{quote(str(getattr(obj, builder.pk_name)), safe='')}",
            csrf=app.csrf.token(request, ctx.principal),
        )

    async def form_page(request: Request, ctx: Context, id: str | None = None) -> Any:
        obj = await service._get(id, ctx, "update") if id is not None else None
        action = "create" if obj is None else "update"
        await service.authorize(action, ctx, obj)
        controls = await form_fields(builder, service, ctx, obj)
        fieldsets = fieldset_groups(builder, controls)
        names = tuple(field["name"] for field in controls)
        if obj is None:
            values = initial_values(builder, ctx, names)
        else:
            values = {key: getattr(obj, key) for key in names}
        errors = {}
        status = 200
        inline_groups = []
        if request.method == "POST":
            form = await read_form(request)
            app.csrf.validate(request, ctx.principal, str(form.get("csrf_token", "")))
            if any(len(form.getlist(key)) != 1 for key in form):
                raise BuilderValidationError(
                    {"__all__": ["Duplicate form fields are not permitted."]}
                )
            submitted = {key: str(value) for key, value in form.items() if key != "csrf_token"}
            values = {
                key: value
                for key, value in submitted.items()
                if not key.startswith("inline:")
                and key not in {"_save", "_addanother", "_continue"}
            }
            inline_groups = await inline_rows(ctx, obj, submitted=submitted)
            inline_errors = any(row["errors"] for group in inline_groups for row in group["rows"])
            if not inline_errors:
                written = False
                try:
                    converted = convert_values(builder, values, obj)
                    if obj is None:
                        instance = await service.create(converted, ctx=ctx)
                    else:
                        instance = await service.update(id, converted, ctx=ctx)
                    written = True
                    for group in inline_groups:
                        inline = builder.inline_instances[group["index"]]
                        child_service = app.service(inline.model)
                        for row in group["rows"]:
                            if row["delete"]:
                                await child_service.delete(row["id"], ctx=ctx)
                            elif row["_blank"]:
                                continue
                            elif row["_record"] is None:
                                await child_service.create_inline(
                                    row["_data"],
                                    relation=inline.fk_name,
                                    parent=instance,
                                    ctx=ctx,
                                )
                            else:
                                await child_service.update(row["id"], row["_data"], ctx=ctx)
                    if "_addanother" in submitted:
                        location = f"{base(request)}/new"
                    elif "_continue" in submitted:
                        record = quote(str(getattr(instance, builder.pk_name)), safe="")
                        location = f"{base(request)}/{record}/edit"
                    else:
                        location = base(request) if builder.view.enabled else request.url.path
                    return RedirectResponse(location, status_code=303)
                except BuilderValidationError as exc:
                    if ctx._failed or written:
                        raise
                    errors, status = exc.errors, 422
            else:
                status = 422
        else:
            inline_groups = await inline_rows(ctx, obj)
        extra = builder.form.get_context(ctx, instance=obj, action=action)
        return await page(
            builder.form.template,
            request,
            ctx,
            extra=extra,
            status=status,
            title=extra.get("title", builder.model.__name__),
            base=base(request),
            fieldsets=fieldsets,
            values=values if request.method == "POST" else input_values(builder, values),
            errors=errors,
            inlines=inline_groups,
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
