"""Record fragments reuse core CRUD, field controls, and activity services."""

import contextlib
import json
import logging
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, urlencode
from uuid import UUID, uuid4

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import ValidationError
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException
from starlette.middleware.exceptions import ExceptionMiddleware
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.types import Message, Receive, Scope, Send

from obox_builder.builder.api import record_etag
from obox_builder.builder.conditional import check_match
from obox_builder.builder.http import accepts_json, authentication_response, read_form
from obox_builder.builder.query import parameters
from obox_builder.builder.records import relationship
from obox_builder.builder.service import invoke
from obox_builder.builder.ui import allowed_operations, form_fields, input_values
from obox_builder.context import Context
from obox_builder.contrib.activity.models import Comment
from obox_builder.contrib.activity.schemas import CommentInput
from obox_builder.contrib.activity.services import comment, comment_etag, comment_row, own, read
from obox_builder.errors import BuilderValidationError, NotFound, OboxError, PermissionDenied
from obox_builder.i18n import context as translation_context
from obox_builder.i18n import localize, negotiate


class FormFailure(OboxError):
    def __init__(self, response: HTMLResponse) -> None:
        self.response = response


class RecordRoute(APIRoute):
    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope)
        provided = request.headers.get("x-request-id", "")
        request.state.trace_id = (
            provided if re.fullmatch(r"[A-Za-z0-9._-]{1,128}", provided) else uuid4().hex
        )
        request.state.language = getattr(
            request.state, "language", negotiate(request.headers.get("accept-language", ""))
        )

        async def error(request: Request, exc: Exception) -> Any:
            if isinstance(exc, FormFailure):
                return exc.response
            status = (
                exc.status
                if isinstance(exc, OboxError)
                else exc.status_code
                if isinstance(exc, HTTPException)
                else 422
                if isinstance(exc, RequestValidationError)
                else 500
            )
            if status == 500:
                logging.getLogger(__name__).error("Record page request failed.", exc_info=exc)
            response = scope["app"].state.obox.templates.TemplateResponse(
                request=request,
                name="obox/record.html",
                status_code=status,
                context={
                    "fragment": "obox/_record_error.html",
                    "message": str(exc) if status < 500 else "An unexpected error occurred.",
                    "full": request.headers.get("x-obox-fragment") != "true",
                    "base_template": scope["app"].state.obox.settings.base_template,
                    "title": "Request failed",
                    "navigation": [],
                    **translation_context(request),
                },
            )
            return authentication_response(request, exc, response, ui=True)

        async def respond(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Cache-Control"] = "private, no-store"
                headers["X-Request-ID"] = request.state.trace_id
                headers["Content-Language"] = request.state.language
                headers.add_vary_header(
                    "Cookie, Authorization, Accept-Language, X-Obox-Fragment, X-Obox-Request"
                )
            await send(message)

        async def dispatch(scope: Scope, receive: Receive, send: Send) -> None:
            if not accepts_json(request.headers.get("accept", "*/*"), "text/html"):
                raise HTTPException(406, "Use text/html.")
            if (
                request.method == "POST"
                and request.headers.get("content-type", "").split(";", 1)[0].strip()
                != "application/x-www-form-urlencoded"
            ):
                raise HTTPException(415, "Use a URL-encoded form.")
            await APIRoute.handle(self, scope, receive, send)

        await ExceptionMiddleware(
            dispatch,
            handlers={
                Exception: error,
                OboxError: error,
                HTTPException: error,
                RequestValidationError: error,
            },
        )(scope, receive, respond)


def build_record_ui(
    app: Any, builder: Any, service: Any, page: Any, base: Callable[[Request], str]
) -> tuple[APIRouter, Any, Any]:
    router = APIRouter(route_class=RecordRoute, include_in_schema=False)
    path = f"{builder.ui_path}/{{id}}"
    dependency = Depends(app.context_dependency("ui"), scope="function")
    activity = app.get_plugin("activity")

    def url(request: Request, id: str) -> str:
        return f"{base(request)}/{quote(id, safe='')}"

    async def render(
        request: Request,
        ctx: Context,
        fragment: str,
        *,
        status: int = 200,
        full: bool = False,
        instance: Any = None,
        **values: Any,
    ) -> Any:
        context = {
            "fragment": fragment,
            "full": full,
            "title": builder.model.__name__,
            "base": base(request),
            "csrf": app.csrf.token(request, ctx.principal),
            "errors": {},
            "message": "",
            "body": "",
            **values,
        }
        if full and status == 200:
            template = builder.view.detail_template
            return await page(
                "obox/record.html" if template == "obox/detail.html" else template,
                request,
                ctx,
                extra=builder.view.get_context(ctx, instance=instance),
                **context,
            )
        return app.templates.TemplateResponse(
            request=request,
            name="obox/record.html",
            context={
                "base_template": app.settings.base_template,
                "navigation": [],
                "principal": ctx.principal,
                **translation_context(request),
                **context,
            },
            status_code=status,
        )

    async def form(request: Request, ctx: Context, names: set[str]) -> dict[str, str]:
        parameters(request.query_params.multi_items())
        for name in ("If-Match", "If-None-Match", "If-Unmodified-Since"):
            if name in request.headers:
                raise BuilderValidationError({name: ["Use the form's if_match control."]})
        values = await read_form(request)
        if set(values) - names or any(len(values.getlist(key)) != 1 for key in values):
            raise BuilderValidationError({"__all__": ["Unexpected or repeated form controls."]})
        app.csrf.validate(request, ctx.principal, str(values.get("csrf_token", "")))
        return {key: str(value) for key, value in values.items()}

    async def field_data(
        request: Request,
        id: str,
        field: str,
        ctx: Context,
        *,
        editing: bool = False,
        obj: Any = None,
    ) -> dict[str, Any]:
        obj = await service.get(id, ctx=ctx) if obj is None else obj
        if field not in builder.view_details:
            raise NotFound("Field is unavailable.")
        visible = await service.serialize(obj, ctx=ctx, fields=(field,))
        if field not in visible:
            raise PermissionDenied("Field is not readable.")
        editable = (
            builder.version_column is not None
            and builder.view.inline_edit is not False
            and builder.form.enabled
            and "update" in await allowed_operations(builder, ctx, obj)
            and field in service.write_fields("update", ctx, obj)
        )
        if editing:
            if not editable:
                raise PermissionDenied("Field is not editable.")
            await service._get(id, ctx, "update")
        spec = builder.specs[field]
        value, link = visible[field], None
        unavailable = False
        if value is not None and spec.column.foreign_keys:
            unavailable = True
            value = "Unavailable"
            fk = next(iter(spec.column.foreign_keys))
            target = app._for_table(fk.column.table)
            if target is not None and await invoke(target.policy.allow, "retrieve", ctx):
                remote = sa.inspect(target.model).get_property_by_column(fk.column).key
                stmt = (
                    app.service(target.model)
                    ._statement("retrieve", ctx)
                    .where(getattr(target.model, remote) == visible[field])
                )
                child = (await ctx.session.scalars(stmt)).unique().one_or_none()
                if child is not None and await invoke(
                    target.policy.allow, "retrieve", ctx, obj=child
                ):
                    fields = target.readable(ctx, obj=child)
                    allowed = target.policy.read_fields(ctx, obj=child)
                    if allowed is None or target.pk_name in allowed:
                        fields = (*fields, target.pk_name)
                    if target.pk_name in fields and remote in fields:
                        unavailable = False
                        label = (
                            getattr(child, target.label_field)
                            if target.label_field in fields
                            else None
                        )
                        value = str(
                            getattr(child, target.pk_name)
                            if label is None or label == ""
                            else label
                        )
                        if target.view.enabled:
                            key = quote(str(getattr(child, target.pk_name)), safe="")
                            link = localize(request, f"{target.ui_path}/{key}")
        control = None
        initial = input_values(builder, visible).get(field, "")
        if editing:
            controls = await form_fields(builder, service, ctx, obj, names=(field,), selectors=True)
            if not controls:
                raise PermissionDenied("The field editor is unavailable.")
            control = {**controls[0], "name": "value"}
            if control["config"].get("options_url") and not unavailable and initial:
                control["options"] = [(initial, value)]
        return {
            "field": field,
            "label": builder.form.labels.get(field, field.replace("_", " ").capitalize()),
            "value": value,
            "link": link,
            "editable": editable,
            "editor": editing,
            "control": control,
            "initial": initial if not unavailable else "",
            "set_null": visible[field] is None,
            "if_match": await record_etag(builder, service, obj, ctx) if editing else "",
            "record_url": url(request, id),
            "nullable": spec.column.nullable,
            "file_config": app.get_plugin("files").form_config(builder, field, obj, ctx)
            if "obox_files" in spec.column.info
            else None,
        }

    async def timeline(
        request: Request, id: str, ctx: Context, params: dict[str, str] | None = None
    ) -> dict[str, Any]:
        if activity is None:
            raise NotFound("Activity is unavailable.")
        params = dict(params or {})
        if "limit" not in params and builder.view.page_size is not None:
            params["limit"] = str(builder.view.page_size)
        result = await read(activity, builder, id, params, ctx, fields=builder.view_details)
        ids = [UUID(item["comment"]["id"]) for item in result["items"] if item["kind"] == "comment"]
        versions = (
            {
                str(obj.id): obj
                for obj in await ctx.session.scalars(sa.select(Comment).where(Comment.id.in_(ids)))
            }
            if ids
            else {}
        )
        for item in result["items"]:
            if item["kind"] == "comment":
                row = item["comment"]
                row["etag"] = comment_etag(versions[row["id"]], row)
        can_comment = False
        if getattr(ctx.principal, "is_authenticated", False) and getattr(
            ctx.principal, "is_active", True
        ):
            try:
                await service._get(id, ctx, "comment")
                can_comment = True
            except (PermissionDenied, NotFound):
                pass
        next_cursor = result["page"]["next_cursor"]
        return {
            "events": result["items"],
            "can_comment": can_comment,
            "record_url": url(request, id),
            "next_url": url(request, id)
            + "/activity?"
            + urlencode({**params, "cursor": next_cursor})
            if next_cursor
            else None,
        }

    async def record_page(request: Request, id: str, ctx: Context, *, full: bool = True) -> Any:
        parameters(request.query_params.multi_items())
        obj = await service.get(id, ctx=ctx)
        fields = [
            await field_data(request, id, name, ctx, obj=obj)
            for name in builder.readable(ctx, obj=obj, fields=builder.view_details)
        ]
        cards = []
        for name in builder.view_related:
            with contextlib.suppress(PermissionDenied, NotFound):
                cards.append(await relationship(app, builder, id, name, {}, ctx))
        events = None
        if activity is not None:
            with contextlib.suppress(PermissionDenied, NotFound):
                events = await timeline(request, id, ctx)
        operations = await allowed_operations(builder, ctx, obj)
        return await render(
            request,
            ctx,
            "obox/_record_content.html",
            full=full,
            instance=obj,
            record_id=id,
            edit_url=url(request, id) + "/edit"
            if builder.form.enabled
            and "update" in operations
            and (builder.version_column is None or builder.view.inline_edit is False)
            else None,
            delete_url=url(request, id) + "/delete" if "delete" in operations else None,
            fields=fields,
            cards=cards,
            activity=events,
            record_url=url(request, id),
        )

    @router.get(path + "/fields/{field}", name=f"obox_ui_{builder.route_key}_field")
    async def field_value(request: Request, id: str, field: str, ctx: Context = dependency) -> Any:
        parameters(request.query_params.multi_items())
        return await render(
            request,
            ctx,
            "obox/_record_field.html",
            field_data=await field_data(request, id, field, ctx),
        )

    @router.get(path + "/fields/{field}/edit", name=f"obox_ui_{builder.route_key}_field_edit")
    async def field_edit(request: Request, id: str, field: str, ctx: Context = dependency) -> Any:
        parameters(request.query_params.multi_items())
        return await render(
            request,
            ctx,
            "obox/_record_field.html",
            field_data=await field_data(request, id, field, ctx, editing=True),
        )

    @router.post(path + "/fields/{field}", name=f"obox_ui_{builder.route_key}_field_save")
    async def field_save(request: Request, id: str, field: str, ctx: Context = dependency) -> Any:
        values = await form(request, ctx, {"csrf_token", "if_match", "value", "set_null"})
        data = await field_data(request, id, field, ctx, editing=True)
        data.update(
            initial=values.get("value", ""),
            if_match=values.get("if_match", ""),
            set_null=values.get("set_null") == "1",
        )
        try:
            if values.get("set_null", "0") not in {"0", "1"} or (
                not data["set_null"] and "value" not in values
            ):
                raise BuilderValidationError({field: ["Supply a field value."]})
            value: Any = None if data["set_null"] else values["value"]
            if value is not None and isinstance(builder.specs[field].column.type, sa.JSON):
                try:
                    value = json.loads(value)
                except ValueError as exc:
                    raise BuilderValidationError({field: ["Enter valid JSON."]}) from exc

            async def condition(obj: Any) -> None:
                check_match(
                    [values["if_match"]] if "if_match" in values else [],
                    await record_etag(builder, service, obj, ctx),
                    single=True,
                )

            await service.update(id, {field: value}, ctx=ctx, precondition=condition)
        except OboxError as exc:
            response = await render(
                request,
                ctx,
                "obox/_record_field.html",
                status=exc.status,
                full=request.headers.get("x-obox-fragment") != "true",
                field_data=data,
                errors={"value": getattr(exc, "errors", {}).get(field, [])},
                message=str(exc),
            )
            raise FormFailure(response) from exc
        if request.headers.get("x-obox-fragment") == "true":
            return await record_page(request, id, ctx, full=False)
        return RedirectResponse(url(request, id), status_code=303)

    @router.get(path + "/relationships/{field}", name=f"obox_ui_{builder.route_key}_relationship")
    async def related(request: Request, id: str, field: str, ctx: Context = dependency) -> Any:
        params = parameters(request.query_params.multi_items(), {"limit", "cursor"})
        data = await relationship(app, builder, id, field, params, ctx)
        cursor = data["page"]["next_cursor"]
        return await render(
            request,
            ctx,
            "obox/_record_relationship.html",
            card=data,
            record_url=url(request, id),
            next_url=url(request, id)
            + "/relationships/"
            + field
            + "?"
            + urlencode({**params, "limit": data["page"]["limit"], "cursor": cursor})
            if cursor
            else None,
        )

    if activity is not None:

        @router.get(path + "/activity", name=f"obox_ui_{builder.route_key}_activity")
        async def activity_page(request: Request, id: str, ctx: Context = dependency) -> Any:
            params = parameters(request.query_params.multi_items(), {"kind", "limit", "cursor"})
            return await render(
                request,
                ctx,
                "obox/_record_activity.html",
                **await timeline(request, id, ctx, params),
            )

        async def comment_data(
            request: Request, id: str, comment_id: str, ctx: Context
        ) -> dict[str, Any]:
            obj = await comment(activity, builder, id, ctx, comment_id=comment_id)
            if not own(obj, ctx):
                raise PermissionDenied("Only the author may edit this comment.")
            return {
                "comment_id": comment_id,
                "body": obj.body,
                "if_match": comment_etag(obj, await comment_row(activity, obj, ctx)),
                "record_url": url(request, id),
            }

        @router.get(
            path + "/comments/{comment_id}/edit", name=f"obox_ui_{builder.route_key}_comment_edit"
        )
        async def comment_editor(
            request: Request, id: str, comment_id: str, ctx: Context = dependency
        ) -> Any:
            parameters(request.query_params.multi_items())
            return await render(
                request,
                ctx,
                "obox/_record_comment.html",
                **await comment_data(request, id, comment_id, ctx),
            )

        async def mutate_comment(
            request: Request, id: str, ctx: Context, action: str, comment_id: str | None = None
        ) -> Any:
            names = (
                {"csrf_token", "body"}
                if action == "create"
                else {"csrf_token", "if_match"} | ({"body"} if action == "update" else set())
            )
            values = await form(request, ctx, names)
            data = (
                await timeline(request, id, ctx)
                if action == "create"
                else await comment_data(request, id, comment_id or "", ctx)
            )
            data["if_match"] = values.get("if_match", "")
            if action != "delete":
                data["body"] = values.get("body", "")
            try:
                body = (
                    CommentInput.model_validate({"body": values.get("body")}).body
                    if action != "delete"
                    else None
                )
                await comment(
                    activity,
                    builder,
                    id,
                    ctx,
                    comment_id=comment_id,
                    action=action,
                    body=body,
                    conditions=[values["if_match"]] if "if_match" in values else [],
                    single=True,
                )
            except (OboxError, ValidationError) as exc:
                response = await render(
                    request,
                    ctx,
                    "obox/_record_activity.html"
                    if action == "create"
                    else "obox/_record_comment.html",
                    status=exc.status if isinstance(exc, OboxError) else 422,
                    full=request.headers.get("x-obox-fragment") != "true",
                    **data,
                    message=str(exc)
                    if isinstance(exc, OboxError)
                    else "Enter a nonempty plain-text comment.",
                )
                raise FormFailure(response) from exc
            if request.headers.get("x-obox-fragment") == "true":
                return await render(
                    request, ctx, "obox/_record_activity.html", **await timeline(request, id, ctx)
                )
            return RedirectResponse(url(request, id) + "#activity", status_code=303)

        @router.post(path + "/comments", name=f"obox_ui_{builder.route_key}_comment_create")
        async def comment_create(request: Request, id: str, ctx: Context = dependency) -> Any:
            return await mutate_comment(request, id, ctx, "create")

        @router.post(
            path + "/comments/{comment_id}/edit", name=f"obox_ui_{builder.route_key}_comment_save"
        )
        async def comment_update(
            request: Request, id: str, comment_id: str, ctx: Context = dependency
        ) -> Any:
            return await mutate_comment(request, id, ctx, "update", comment_id)

        @router.post(
            path + "/comments/{comment_id}/delete",
            name=f"obox_ui_{builder.route_key}_comment_delete",
        )
        async def comment_delete(
            request: Request, id: str, comment_id: str, ctx: Context = dependency
        ) -> Any:
            return await mutate_comment(request, id, ctx, "delete", comment_id)

    return router, record_page, timeline
