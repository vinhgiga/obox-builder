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
from obox_builder.core.exceptions import NotAuthenticated, PermissionDenied
from obox_builder.db.session import get_db
from obox_builder.fields import QuerySpecError

logger = logging.getLogger("obox.admin.api")


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
            try:
                async with asynccontextmanager(get_db)() as db:
                    request.state.api_db = db
                    response = await handler(request)
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
                logger.exception("Admin API request failed")
                response = problem_response(
                    request, Problem(500, "internal_error", "An unexpected error occurred.")
                )
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
                "Admin API request",
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
