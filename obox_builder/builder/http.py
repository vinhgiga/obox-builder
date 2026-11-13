"""HTTP error envelopes, read-query headers, and unbounded URL-encoded forms."""

import logging
import re
from http import HTTPStatus
from typing import Any
from urllib.parse import parse_qsl
from uuid import uuid4

from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute, iter_route_contexts
from pydantic.alias_generators import to_camel
from starlette._utils import get_route_path
from starlette.datastructures import FormData, MutableHeaders
from starlette.exceptions import HTTPException
from starlette.middleware.exceptions import ExceptionMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Match, NoMatchFound
from starlette.types import Message, Receive, Scope, Send

from obox_builder.builder.api_schema import FieldError, Problem
from obox_builder.builder.query import invalid
from obox_builder.errors import (
    BatchOperationError,
    BuilderValidationError,
    NotAuthenticated,
    OboxError,
)
from obox_builder.i18n import negotiate, request_target, translate


def allow_header(scope: Scope) -> str:
    methods: set[str] = set()
    for route in iter_route_contexts(scope["app"].routes):
        if route.path and route.path_regex.fullmatch(get_route_path(scope)):
            methods.update(route.methods or ())
    if "GET" in methods:
        methods.add("HEAD")
    methods.add("OPTIONS")
    return ", ".join(sorted(methods))


class MethodRoute(APIRoute):
    methods: set[str]

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        if scope.get("method") == "HEAD" and "GET" in self.methods:
            return super().matches({**scope, "method": "GET"})
        return super().matches(scope)

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["method"] == "OPTIONS" and "OPTIONS" not in self.methods:
            await Response(status_code=204, headers={"Allow": allow_header(scope)})(
                scope, receive, send
            )
            return
        if scope["method"] not in self.methods and not (
            scope["method"] == "HEAD" and "GET" in self.methods
        ):
            raise HTTPException(405, "Method Not Allowed", headers={"Allow": allow_header(scope)})
        if scope["method"] == "HEAD" and "GET" in self.methods:
            await self.app(scope, receive, send)
        else:
            await super().handle(scope, receive, send)


class OboxRoute(MethodRoute):
    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        obox = scope["app"].state.obox
        middleware = ExceptionMiddleware(
            super().handle,
            handlers={OboxError: obox._problem, RequestValidationError: obox._validation_problem},
        )

        async def respond(message: Message) -> None:
            if scope["method"] == "HEAD" and message["type"] == "http.response.body":
                message = {**message, "body": b""}
            await send(message)

        await middleware(dict(scope), receive, respond)


def _authentication_challenge(request: Request, auth: object | None) -> str:
    if getattr(auth, "api_tokens", False) and "authorization" in request.headers:
        return 'Bearer realm="obox"'
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
    if getattr(auth, "api_tokens", False) and "authorization" in request.headers:
        return response
    if request.headers.get("x-obox-request") == "true":
        response.headers["X-Obox-Redirect"] = auth.login_url(
            request, next_url=request_target(request)
        )
    elif ui and request.method in {"GET", "HEAD"}:
        return RedirectResponse(
            auth.login_url(request, next_url=request_target(request)),
            status_code=303,
            headers={"Cache-Control": "private, no-store", "Vary": "X-Obox-Request"},
        )
    return response


def accepts_json(value: str, media_type: str = "application/json") -> bool:
    if len(value) > 8192:
        return False
    items = value.split(",")
    if len(items) > 64:
        return False
    matches: list[tuple[int, float]] = []
    for item in items:
        if len(item.strip()) > 256:
            return False
        parts = [part.strip().lower() for part in item.split(";")]
        media = parts[0]
        if not re.fullmatch(r"(?:[a-z0-9!#$&^_.+-]+|\*)/(?:[a-z0-9!#$&^_.+-]+|\*)", media):
            continue
        parameters = [part.partition("=") for part in parts[1:]]
        if any(
            not name.strip() or not separator or not operand.strip()
            for name, separator, operand in parameters
        ):
            continue
        qualities = [operand.strip() for name, _, operand in parameters if name.strip() == "q"]
        if len(qualities) > 1 or any(
            not re.fullmatch(r"(?:0(?:\.[0-9]{0,3})?|1(?:\.0{0,3})?)", q) for q in qualities
        ):
            continue
        quality = float(qualities[0]) if qualities else 1.0
        if media == media_type:
            matches.append((2, quality))
        elif media == media_type.split("/")[0] + "/*":
            matches.append((1, quality))
        elif media == "*/*":
            matches.append((0, quality))
    return bool(matches) and max(matches)[1] > 0


async def api_problem(request: Request, exc: Exception) -> Response:
    batch_index = None
    if isinstance(exc, BatchOperationError):
        batch_index, exc = exc.index, exc.error
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
                if location:
                    field = fields.get(location[0]) if fields is not None else None
                    location[0] = (
                        field.alias
                        if field and field.alias
                        else names.get(location[0], to_camel(location[0]))
                    )
            elif location:
                location[0] = names.get(location[0], location[0])
            if batch_index is not None:
                location = [
                    "operations",
                    str(batch_index),
                    *(
                        ["ifMatch"]
                        if name == "If-Match"
                        else ["mergeOn"]
                        if name == "merge_on"
                        else ["data", *location]
                        if location
                        else []
                    ),
                ]
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
            if (
                request.url.path.endswith(":batchWrite")
                and len(location) > 2
                and location[0] == "operations"
                and location[2] in {"create", "update", "delete", "upsert"}
            ):
                del location[2]
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
    if batch_index is not None and not errors:
        errors.append(
            FieldError(
                field=f"operations.{batch_index}",
                pointer=f"#/operations/{batch_index}",
                code=code,
                detail=translate(request, "Internal server error." if status == 500 else str(exc)),
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


class ApiRoute(MethodRoute):
    cache_control: str | None = None
    binary_upload = False
    download = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        responses = {
            status: {
                "content": {
                    "application/problem+json": {"schema": {"$ref": "#/components/schemas/Problem"}}
                }
            }
            for status in (400, 401, 403, 404, 405, 406, 409, 412, 415, 422, 428, 500)
        }
        kwargs["responses"] = {**responses, **(kwargs.get("responses") or {})}
        super().__init__(*args, **kwargs)

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope = dict(scope)
        scope["state"] = dict(scope.get("state", {}))
        request = Request(scope)
        request.state.query_body = request.method == "POST" and self.path.endswith(":query")
        request.state.action_write = (
            request.method == "POST" and ":" in self.path and not self.path.endswith(":query")
        )
        request.state.record_write = (
            request.method in {"POST", "PUT", "PATCH", "DELETE"} and not request.state.query_body
        )
        json_body = not self.binary_upload and (
            request.state.query_body or (request.state.record_write and self.body_field is not None)
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
            if request.method == "HEAD" and message["type"] == "http.response.body":
                message = {**message, "body": b""}
            await send(message)

        async def dispatch(scope: Scope, receive: Receive, send: Send) -> None:
            if request.method == "OPTIONS":
                await MethodRoute.handle(self, scope, receive, send)
                return
            if not self.download and not accepts_json(request.headers.get("accept", "*/*")):
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
            await MethodRoute.handle(self, scope, receive, send)

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
