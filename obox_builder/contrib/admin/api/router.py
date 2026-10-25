"""Build a separately mounted, opt-in admin API."""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from obox_builder.apps import apps
from obox_builder.conf import settings
from obox_builder.contrib.admin.api.auth import register_auth
from obox_builder.contrib.admin.api.deps import DB
from obox_builder.contrib.admin.api.problems import Problem, ProblemDocument, ProblemRoute
from obox_builder.contrib.admin.api.ratelimit import MemoryRateLimiter, RateLimiter
from obox_builder.contrib.admin.api.records import register_records

if TYPE_CHECKING:
    from obox_builder.contrib.admin.site import AdminSite


def build_api_router(site: AdminSite, *, rate_limiter: RateLimiter | None = None) -> APIRouter:
    limiter = rate_limiter if rate_limiter is not None else MemoryRateLimiter()

    async def initialize(request: Request, db: DB) -> None:
        request.state.admin_site = site
        request.state.api_limiter = limiter
        origin = request.headers.get("origin")
        if origin and origin != "*" and origin in settings.ADMIN_API_TRUSTED_ORIGINS:
            request.state.api_cors_origin = origin
        if apps.is_installed("obox_builder.contrib.history"):
            from obox_builder.contrib.history.context import bind

            bind(db, request, source="api")

    router = APIRouter(
        tags=["admin-api"],
        route_class=ProblemRoute,
        dependencies=[Depends(initialize)],
        responses={
            status: {
                "description": HTTPStatus(status).phrase,
                "content": {
                    "application/problem+json": {"schema": ProblemDocument.model_json_schema()}
                },
            }
            for status in (400, 401, 403, 404, 409, 412, 413, 415, 422, 428, 429, 500)
        },
    )
    register_auth(router)
    register_records(router)

    @router.options("/{path:path}", include_in_schema=False)
    async def preflight(request: Request, path: str) -> Response:
        origin = request.headers.get("origin", "")
        if not origin or origin == "*" or origin not in settings.ADMIN_API_TRUSTED_ORIGINS:
            raise Problem(403, "csrf_failed", "Origin is not trusted.")
        request.state.api_cors_origin = origin
        return Response(
            status_code=204,
            headers={
                "Access-Control-Allow-Methods": "GET, POST, PATCH, DELETE, OPTIONS",
                "Access-Control-Allow-Headers": (
                    "Content-Type, X-CSRF-Token, If-Match, Idempotency-Key, X-Delete-Confirmation"
                ),
                "Access-Control-Max-Age": "600",
            },
        )

    @router.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PATCH", "DELETE", "PUT", "HEAD"],
        include_in_schema=False,
    )
    async def missing(path: str) -> None:
        raise Problem(404, "not_found", "Endpoint is unavailable.")

    return router
