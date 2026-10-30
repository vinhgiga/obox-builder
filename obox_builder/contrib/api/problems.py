"""API-local exception translation; HTML handlers remain independent."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Coroutine
from contextlib import asynccontextmanager
from http import HTTPStatus
from typing import Any

from fastapi import Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError
from starlette.exceptions import HTTPException

from obox_builder.contrib.auth.sessions import set_session_cookie
from obox_builder.contrib.comments.exceptions import (
    CommentConflict,
    CommentError,
    CommentNotFound,
    CommentPermissionDenied,
    CommentRateLimited,
    CommentValidationError,
)
from obox_builder.contrib.uploads import discard_uploads
from obox_builder.db.session import get_db
from obox_builder.exceptions import NotAuthenticated, PermissionDenied
from obox_builder.fields import QuerySpecError
from obox_builder.resources import ResourceConflict, ResourceValidationError

logger = logging.getLogger("obox.api")


class ProblemDocument(BaseModel):
    type: str
    title: str
    status: int
    code: str
    detail: str
    instance: str
    request_id: str | None = None
    errors: dict[str, Any] | None = None


class Problem(Exception):
    def __init__(self, status: int, code: str, detail: str, **extra: Any) -> None:
        super().__init__(detail)
        self.status, self.code, self.detail, self.extra = status, code, detail, extra


def problem_response(request: Request, exc: Problem) -> JSONResponse:
    headers = {"WWW-Authenticate": "Session"} if exc.status == 401 else {}
    if exc.status == 401 and request.headers.get("authorization"):
        headers["WWW-Authenticate"] = 'Bearer realm="admin-api", error="invalid_token"'
    if exc.status == 429:
        headers["Retry-After"] = str(exc.extra.get("retry_after", 1))
        headers["RateLimit"] = f'"default";r=0;t={headers["Retry-After"]}'
    return JSONResponse(
        {
            "type": f"urn:obox:problem:{exc.code}",
            "title": HTTPStatus(exc.status).phrase,
            "status": exc.status,
            "code": exc.code,
            "detail": exc.detail,
            "instance": request.url.path,
            "request_id": getattr(request.state, "request_id", None),
            **exc.extra,
        },
        status_code=exc.status,
        media_type="application/problem+json",
        headers=headers,
    )


class ProblemRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def handle(request: Request) -> Response:
            started = time.perf_counter()
            request.state.api_uploads = []
            request.state.api_upload_bytes = 0
            try:
                if self.body_field is not None:
                    from obox_builder.conf import settings
                    from obox_builder.contrib.api.deps import json_body

                    multipart = (
                        getattr(self.body_field.field_info, "media_type", "")
                        == "multipart/form-data"
                    )
                    if multipart and not request.headers.get("content-type", "").startswith(
                        "multipart/form-data"
                    ):
                        raise Problem(
                            415, "unsupported_media_type", "A multipart upload is required."
                        )
                    limit = (
                        settings.UPLOAD_MAX_BYTES + 65536
                        if multipart
                        else settings.API_MAX_BODY_BYTES
                    )
                    # Bound the stream before the transport schema decodes it.
                    content = bytearray()
                    async for chunk in request.stream():
                        content.extend(chunk)
                        if len(content) > limit:
                            raise Problem(413, "payload_too_large", "Request body is too large.")
                    request._body = bytes(content)
                    if not multipart:
                        await json_body(request)
                async with asynccontextmanager(get_db)() as db:
                    request.state.api_db = db
                    from obox_builder.apps import apps

                    if apps.is_installed("obox_builder.contrib.history"):
                        from obox_builder.contrib.history.context import bind

                        bind(db, request, source="api")
                    response = await handler(request)
            except CommentError as exc:
                status, code = 422, "validation_error"
                extra: dict[str, Any] = {}
                if isinstance(exc, CommentConflict):
                    status, code = 409, "conflict"
                elif isinstance(exc, CommentNotFound):
                    status, code = 404, "not_found"
                elif isinstance(exc, CommentPermissionDenied):
                    status, code = 403, "permission_denied"
                elif isinstance(exc, CommentRateLimited):
                    status, code, extra = 429, "rate_limited", {"retry_after": exc.retry_after}
                elif isinstance(exc, CommentValidationError):
                    extra = {"errors": {exc.field: [str(exc)]}}
                response = problem_response(request, Problem(status, code, str(exc), **extra))
            except ResourceValidationError as exc:
                response = problem_response(
                    request, Problem(422, "validation_error", str(exc), errors=exc.errors)
                )
            except ResourceConflict as exc:
                response = problem_response(request, Problem(409, "conflict", str(exc)))
            except Problem as exc:
                response = problem_response(request, exc)
            except NotAuthenticated:
                response = problem_response(
                    request, Problem(401, "not_authenticated", "Authentication required.")
                )
            except PermissionDenied as exc:
                response = problem_response(request, Problem(403, "permission_denied", str(exc)))
            except LookupError:
                response = problem_response(
                    request, Problem(404, "not_found", "Record is unavailable.")
                )
            except QuerySpecError as exc:
                response = problem_response(request, Problem(400, "invalid_query", str(exc)))
            except RequestValidationError as exc:
                errors: dict[str, list[str]] = {}
                for error in exc.errors():
                    name = ".".join(str(part) for part in error["loc"][1:]) or "__all__"
                    errors.setdefault(name, []).append(error["msg"])
                response = problem_response(
                    request, Problem(422, "validation_error", "Invalid request.", errors=errors)
                )
            except StaleDataError:
                response = problem_response(
                    request, Problem(412, "precondition_failed", "The record has changed.")
                )
            except IntegrityError:
                response = problem_response(
                    request,
                    Problem(409, "conflict", "A database constraint prevented this operation."),
                )
            except HTTPException as exc:
                response = problem_response(
                    request, Problem(exc.status_code, "http_error", str(exc.detail))
                )
            except Exception:
                logger.exception("API request failed")
                response = problem_response(
                    request, Problem(500, "internal_error", "An unexpected error occurred.")
                )
            except BaseException:
                discard_uploads(request.state.api_uploads)
                raise
            if response.status_code >= 400:
                discard_uploads(request.state.api_uploads)
            response.headers.update(getattr(request.state, "api_rate_headers", {}))
            renewal = getattr(request.state, "api_renew_token", None)
            if renewal and response.status_code < 400 and "set-cookie" not in response.headers:
                set_session_cookie(response, renewal)
            response.headers["Cache-Control"] = "no-store"
            origin = getattr(request.state, "api_cors_origin", None)
            if origin:
                response.headers.update(
                    {
                        "Access-Control-Allow-Origin": origin,
                        "Access-Control-Allow-Credentials": "true",
                        "Access-Control-Expose-Headers": (
                            "ETag, Location, Link, RateLimit, RateLimit-Policy, Retry-After"
                        ),
                    }
                )
                response.headers.append("Vary", "Origin")
            logger.info(
                "API request",
                extra={
                    "principal": getattr(request.state, "api_principal", "anonymous"),
                    "path": request.url.path,
                    "method": request.method,
                    "model": request.path_params.get("label"),
                    "status": response.status_code,
                    "duration": round(time.perf_counter() - started, 4),
                    "request_id": getattr(request.state, "request_id", None),
                },
            )
            return response

        return handle
