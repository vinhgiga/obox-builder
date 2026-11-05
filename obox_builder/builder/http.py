"""HTTP error envelopes, read-query headers, and unbounded URL-encoded forms."""

import logging
import re
from http import HTTPStatus
from urllib.parse import parse_qsl
from uuid import uuid4

from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic.alias_generators import to_camel
from starlette.datastructures import FormData, MutableHeaders
from starlette.exceptions import HTTPException
from starlette.middleware.exceptions import ExceptionMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import Message, Receive, Scope, Send

from obox_builder.builder.api_schema import FieldError, Problem
from obox_builder.builder.query import invalid
from obox_builder.errors import BuilderValidationError, OboxError
from obox_builder.i18n import negotiate, translate


class OboxRoute(APIRoute):
    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        obox = scope["app"].state.obox
        middleware = ExceptionMiddleware(
            super().handle,
            handlers={OboxError: obox._problem, RequestValidationError: obox._validation_problem},
        )
        await middleware(dict(scope), receive, send)


async def api_problem(request: Request, exc: Exception) -> JSONResponse:
    status = (
        exc.status
        if isinstance(exc, OboxError)
        else (
            exc.status_code
            if isinstance(exc, HTTPException)
            else 422
            if isinstance(exc, RequestValidationError)
            else 500
        )
    )
    if isinstance(exc, RequestValidationError) and any(
        error["type"] == "json_invalid" for error in exc.errors()
    ):
        status = 400
    code = getattr(exc, "code", None) if isinstance(exc, OboxError) and status != 500 else None
    code = code or {
        400: "malformed_json",
        401: "unauthenticated",
        403: "permission_denied",
        404: "not_found",
        409: "resource_conflict",
        415: "unsupported_media_type",
        500: "internal_error",
    }.get(status, "validation_error")
    errors: list[FieldError] = []
    if isinstance(exc, BuilderValidationError):
        for name, messages in exc.errors.items():
            location = [name]
            if request.state.record_write:
                location = name.split(".") if name != "__all__" else []
                fields = getattr(request.state, "record_fields", None)
                if location and fields is not None:
                    field = fields.get(location[0])
                    location[0] = field.alias if field and field.alias else to_camel(location[0])
            pointer = "#" + "".join(
                "/" + part.replace("~", "~0").replace("/", "~1") for part in location
            )
            errors.extend(
                FieldError(
                    field=".".join(location) if location else "__all__",
                    pointer="#/query"
                    if request.state.query_body
                    else "#"
                    if request.method == "DELETE"
                    else pointer,
                    code=(exc.field_code or code) if request.state.record_write else code,
                    detail=translate(request, message),
                )
                for message in messages
            )
    elif isinstance(exc, RequestValidationError):
        for error in exc.errors():
            location = [str(part) for part in error["loc"][1:]]
            errors.append(
                FieldError(
                    field=".".join(location) if location else "__all__",
                    pointer="#"
                    + "".join(
                        "/" + part.replace("~", "~0").replace("/", "~1") for part in location
                    ),
                    code={"missing": "required", "extra_forbidden": "unknown_field"}.get(
                        error["type"], "invalid_type"
                    ),
                    detail=translate(request, error["msg"]),
                )
            )
    if status == 500:
        logging.getLogger(__name__).error(
            "API request %s failed", request.state.trace_id, exc_info=exc
        )
    return JSONResponse(
        Problem(
            type="urn:obox:problem:" + code,
            title="Unprocessable Content" if status == 422 else HTTPStatus(status).phrase,
            status=status,
            code=code,
            detail=translate(
                request,
                "Internal server error."
                if status == 500
                else "Invalid input."
                if isinstance(exc, RequestValidationError)
                else str(exc.detail)
                if isinstance(exc, HTTPException)
                else str(exc),
            ),
            instance=request.url.path,
            trace_id=request.state.trace_id,
            errors=errors,
        ).model_dump(mode="json", exclude_unset=True),
        status_code=status,
        media_type="application/problem+json",
        headers=exc.headers if isinstance(exc, HTTPException) else None,
    )


class ApiRoute(APIRoute):
    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope = dict(scope)
        scope["state"] = dict(scope.get("state", {}))
        request = Request(scope)
        request.state.query_body = request.method == "POST" and self.path.endswith(":query")
        request.state.record_write = request.method in {"PATCH", "DELETE"} or (
            request.method == "POST" and self.status_code == 201
        )
        json_body = request.state.query_body or (
            request.state.record_write and request.method != "DELETE"
        )
        no_store = json_body or request.state.record_write
        provided = request.headers.get("x-request-id", "")
        request.state.trace_id = (
            provided if re.fullmatch(r"[A-Za-z0-9._-]{1,128}", provided) else uuid4().hex
        )
        request.state.language = negotiate(request.headers.get("accept-language", ""))

        async def respond(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request.state.trace_id
                headers["Cache-Control"] = "private, no-store" if no_store else "private, no-cache"
                headers.add_vary_header("Cookie, Authorization, Accept-Language")
                headers["Content-Language"] = request.state.language
            await send(message)

        async def dispatch(scope: Scope, receive: Receive, send: Send) -> None:
            if request.method in {"PATCH", "DELETE"}:
                for name in ("If-Match", "If-None-Match", "If-Unmodified-Since"):
                    if name in request.headers:
                        raise invalid(name, "Unsupported precondition.", "unsupported_precondition")
            if json_body and (
                request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                != "application/json"
            ):
                raise HTTPException(415, "Use application/json.")
            if no_store and request.query_params:
                raise invalid(
                    next(iter(request.query_params)),
                    "Query string must be empty.",
                    "unknown_parameter",
                )
            await APIRoute.handle(self, scope, receive, send)

        await ExceptionMiddleware(
            dispatch,
            handlers={
                Exception: api_problem,
                HTTPException: api_problem,
                RequestValidationError: api_problem,
            },
        )(scope, receive, respond)


async def read_form(request: Request) -> FormData:
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip()
        != "application/x-www-form-urlencoded"
    ):
        raise BuilderValidationError({"__all__": ["Use a URL-encoded form."]})
    try:
        body = (await request.body()).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BuilderValidationError({"__all__": ["Use UTF-8 form data."]}) from exc
    return FormData([(key, value) for key, value in parse_qsl(body, keep_blank_values=True)])
