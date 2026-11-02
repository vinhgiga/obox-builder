"""Unbounded URL-encoded forms; file uploads are outside the core contract."""

from urllib.parse import parse_qsl

from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from starlette.datastructures import FormData
from starlette.middleware.exceptions import ExceptionMiddleware
from starlette.requests import Request
from starlette.types import Receive, Scope, Send

from obox_builder.errors import BuilderValidationError, OboxError


class OboxRoute(APIRoute):
    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        obox = scope["app"].state.obox
        middleware = ExceptionMiddleware(
            super().handle,
            handlers={OboxError: obox._problem, RequestValidationError: obox._validation_problem},
        )
        await middleware(dict(scope), receive, send)


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
