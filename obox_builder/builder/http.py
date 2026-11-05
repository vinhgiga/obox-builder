"""HTTP error envelopes, read-query headers, and unbounded URL-encoded forms."""

import logging
import re
from http import HTTPStatus
from urllib.parse import parse_qsl
from uuid import uuid4

from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
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
    code = getattr(
        exc,
        "code",
        {
            400: "malformed_json",
            401: "unauthenticated",
            403: "permission_denied",
            404: "not_found",
            415: "unsupported_media_type",
            500: "internal_error",
        }.get(status, "validation_error"),
    )
    errors: list[FieldError] = []
    if isinstance(exc, BuilderValidationError):
        errors = [
            FieldError(
                field=name,
                pointer="#/query"
                if request.state.query_body
                else "#/" + name.replace("~", "~0").replace("/", "~1"),
                code=code,
                detail=translate(request, message),
            )
            for name, messages in exc.errors.items()
            for message in messages
        ]
    elif isinstance(exc, RequestValidationError):
        for error in exc.errors():
            location = [str(part) for part in error["loc"][1:]]
            errors.append(
                FieldError(
                    field=".".join(location),
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
        provided = request.headers.get("x-request-id", "")
        request.state.trace_id = (
            provided if re.fullmatch(r"[A-Za-z0-9._-]{1,128}", provided) else uuid4().hex
        )
        request.state.language = negotiate(request.headers.get("accept-language", ""))

        async def respond(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request.state.trace_id
                headers["Cache-Control"] = (
                    "private, no-store" if request.state.query_body else "private, no-cache"
                )
                headers.add_vary_header("Cookie, Authorization, Accept-Language")
                headers["Content-Language"] = request.state.language
            await send(message)

        async def dispatch(scope: Scope, receive: Receive, send: Send) -> None:
            if request.state.query_body:
                if (
                    request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    != "application/json"
                ):
                    raise HTTPException(415, "Use application/json.")
                if request.query_params:
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
