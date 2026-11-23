"""Shared form body parsing for native requests."""

from urllib.parse import parse_qsl

from fastapi import Request
from starlette.datastructures import FormData

from obox.core.exceptions import BuilderValidationError


def form_media_type(request: Request) -> str:
    return request.headers.get("content-type", "").split(";", 1)[0].strip().lower()


async def read_form(request: Request) -> FormData:
    if form_media_type(request) != "application/x-www-form-urlencoded":
        raise BuilderValidationError({"__all__": ["Use a URL-encoded form."]})
    try:
        body = (await request.body()).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BuilderValidationError({"__all__": ["Use UTF-8 form data."]}) from exc
    return FormData([(key, value) for key, value in parse_qsl(body, keep_blank_values=True)])


async def form_input(request: Request) -> dict[str, FormData]:
    if form_media_type(request) != "multipart/form-data":
        return {"data": await read_form(request)}
    parts = (await request.form()).multi_items()
    return {
        "data": FormData([(key, value) for key, value in parts if isinstance(value, str)]),
        "files": FormData([(key, value) for key, value in parts if not isinstance(value, str)]),
    }
