"""Unbounded URL-encoded forms; file uploads are outside the core contract."""

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


async def list_problem(request: Request, exc: Exception) -> JSONResponse:
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
    code = getattr(
        exc,
        "code",
        {
            401: "unauthenticated",
            403: "permission_denied",
            404: "not_found",
            500: "internal_error",
        }.get(status, "validation_error"),
    )
    errors = []
    if isinstance(exc, BuilderValidationError):
        errors = [
            {
                "field": name,
                "pointer": "#/" + name.replace("~", "~0").replace("/", "~1"),
                "code": code,
                "detail": translate(request, message),
            }
            for name, messages in exc.errors.items()
            for message in messages
        ]
    elif isinstance(exc, RequestValidationError):
        errors = [
            {
                "field": str(error["loc"][-1]),
                "pointer": "#",
                "code": "invalid_type",
                "detail": translate(request, error["msg"]),
            }
            for error in exc.errors()
        ]
    if status == 500:
        logging.getLogger(__name__).error(
            "List request %s failed", request.state.trace_id, exc_info=exc
        )
    return JSONResponse(
        {
            "type": "urn:obox:problem:" + code,
            "title": "Unprocessable Content" if status == 422 else HTTPStatus(status).phrase,
            "status": status,
            "code": code,
            "detail": translate(
                request,
                "Internal server error."
                if status == 500
                else str(exc.detail)
                if isinstance(exc, HTTPException)
                else str(exc),
            ),
            "instance": request.url.path,
            "trace_id": request.state.trace_id,
            "errors": errors,
        },
        status_code=status,
        media_type="application/problem+json",
        headers=exc.headers if isinstance(exc, HTTPException) else None,
    )


class ListRoute(OboxRoute):
    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope = dict(scope)
        scope["state"] = dict(scope.get("state", {}))
        request = Request(scope)
        provided = request.headers.get("x-request-id", "")
        request.state.trace_id = (
            provided if re.fullmatch(r"[A-Za-z0-9._-]{1,128}", provided) else uuid4().hex
        )
        request.state.language = negotiate(request.headers.get("accept-language", ""))

        async def respond(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request.state.trace_id
                headers["Cache-Control"] = "private, no-cache"
                headers.add_vary_header("Cookie, Authorization, Accept-Language")
                headers["Content-Language"] = request.state.language
            await send(message)

        async def dispatch(scope: Scope, receive: Receive, send: Send) -> None:
            await APIRoute.handle(self, scope, receive, send)

        await ExceptionMiddleware(
            dispatch,
            handlers={
                Exception: list_problem,
                HTTPException: list_problem,
                RequestValidationError: list_problem,
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
