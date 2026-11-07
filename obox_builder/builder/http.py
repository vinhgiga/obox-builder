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
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import NoMatchFound
from starlette.types import Message, Receive, Scope, Send

from obox_builder.builder.api_schema import FieldError, Problem
from obox_builder.builder.query import invalid
from obox_builder.errors import BuilderValidationError, NotAuthenticated, OboxError
from obox_builder.i18n import negotiate, translate


class OboxRoute(APIRoute):
    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        obox = scope["app"].state.obox
        middleware = ExceptionMiddleware(
            super().handle,
            handlers={OboxError: obox._problem, RequestValidationError: obox._validation_problem},
        )
        await middleware(dict(scope), receive, send)


def _authentication_challenge(request: Request, auth: object | None) -> str:
    challenge = 'Cookie realm="obox"'
    cookie_name = getattr(auth, "cookie_name", None)
    if isinstance(cookie_name, str) and cookie_name:
        challenge += f", cookie-name={cookie_name}"
    login_url = getattr(auth, "login_url", None)
    if callable(login_url):
        try:
            url = login_url(request)
        except NoMatchFound:
            pass
        else:
            challenge += f', form-action="{url}"'
    return challenge


def authentication_response(
    request: Request, exc: Exception, response: Response, *, ui: bool = False
) -> Response:
    auth = request.app.state.obox.auth
    if response.status_code == 401 and "WWW-Authenticate" not in response.headers:
        response.headers["WWW-Authenticate"] = _authentication_challenge(request, auth)
    if not isinstance(exc, NotAuthenticated) or auth is None:
        return response
    response.headers["Cache-Control"] = "private, no-store"
    response.headers.add_vary_header("X-Obox-Request")
    if request.headers.get("x-obox-request") == "true":
        response.headers["X-Obox-Redirect"] = auth.login_url(request)
    elif ui and request.method in {"GET", "HEAD"}:
        return RedirectResponse(
            auth.login_url(request),
            status_code=303,
            headers={"Cache-Control": "private, no-store", "Vary": "X-Obox-Request"},
        )
    return response


def accepts_json(value: str, media_type: str = "application/json") -> bool:
    for item in value.split(","):
        parts = [part.strip().lower() for part in item.split(";")]
        if parts[0] not in {"*/*", media_type, media_type.split("/")[0] + "/*"}:
            continue
        if any(part == "q=0" for part in parts[1:]):
            continue
        return True
    return False


async def api_problem(request: Request, exc: Exception) -> Response:
    query_body = getattr(request.state, "query_body", False)
    record_write = getattr(request.state, "record_write", False)
    trace_id = getattr(request.state, "trace_id", uuid4().hex)
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
        405: "method_not_allowed",
        406: "not_acceptable",
        409: "resource_conflict",
        415: "unsupported_media_type",
        500: "internal_error",
    }.get(status, "validation_error")
    errors: list[FieldError] = []
    if isinstance(exc, BuilderValidationError):
        for name, messages in exc.errors.items():
            location = [name]
            names = getattr(request.state, "api_names", {})
            if record_write:
                location = name.split(".") if name != "__all__" else []
                fields = getattr(request.state, "record_fields", None)
                if location and fields is not None:
                    field = fields.get(location[0])
                    location[0] = field.alias if field and field.alias else to_camel(location[0])
            elif location:
                location[0] = names.get(location[0], location[0])
            pointer = "#" + "".join(
                "/" + part.replace("~", "~0").replace("/", "~1") for part in location
            )
            errors.extend(
                FieldError(
                    field=".".join(location) if location else "__all__",
                    pointer="#/query"
                    if query_body
                    else "#"
                    if request.method == "DELETE"
                    else "#"
                    if request.query_params and request.method not in {"POST", "PATCH", "DELETE"}
                    else pointer,
                    code=(exc.field_code or code) if record_write else code,
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
        logging.getLogger(__name__).error("API request %s failed", trace_id, exc_info=exc)
    response = JSONResponse(
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
            trace_id=trace_id,
            errors=errors,
        ).model_dump(mode="json", exclude_unset=True),
        status_code=status,
        media_type="application/problem+json",
        headers=exc.headers if isinstance(exc, HTTPException) else None,
    )
    return authentication_response(request, exc, response)


class ApiRoute(APIRoute):
    cache_control: str | None = None

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope = dict(scope)
        scope["state"] = dict(scope.get("state", {}))
        request = Request(scope)
        request.state.query_body = request.method == "POST" and self.path.endswith(":query")
        request.state.action_write = (
            request.method == "POST" and ":" in self.path and not self.path.endswith(":query")
        )
        request.state.record_write = request.method in {"PATCH", "DELETE"} or (
            request.method == "POST" and (self.status_code == 201 or request.state.action_write)
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
                headers["Cache-Control"] = (
                    self.cache_control
                    or headers.get("Cache-Control")
                    or ("private, no-store" if no_store else "private, no-cache")
                )
                headers.add_vary_header("Cookie, Authorization, Accept-Language")
                headers["Content-Language"] = request.state.language
            await send(message)

        async def dispatch(scope: Scope, receive: Receive, send: Send) -> None:
            if not accepts_json(request.headers.get("accept", "*/*")):
                raise HTTPException(406, "Use an application/json response.")
            if request.method in {"PATCH", "DELETE"}:
                for name in ("If-None-Match", "If-Unmodified-Since"):
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
