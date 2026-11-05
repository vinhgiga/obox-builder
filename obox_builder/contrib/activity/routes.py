"""JSON adapter for the read-only activity plugin."""

from typing import Any

from fastapi import APIRouter, Depends, Request

from obox_builder.builder.api_schema import FieldError, Problem
from obox_builder.builder.http import ApiRoute
from obox_builder.builder.query import invalid
from obox_builder.context import Context
from obox_builder.contrib.activity import Activity
from obox_builder.contrib.activity.schemas import ActivityPage
from obox_builder.contrib.activity.services import read
from obox_builder.errors import NotFound


class ActivityRoute(ApiRoute):
    cache_control = "private, no-store"


def build_router(activity: Activity) -> APIRouter:
    app = activity.app
    router = APIRouter(prefix=app.settings.api_prefix, route_class=ActivityRoute)
    dependency = Depends(app.context_dependency("api"), scope="function")
    problem = Problem.model_json_schema()
    problem["properties"]["errors"]["items"] = FieldError.model_json_schema()
    problem.pop("$defs")

    @router.get(
        "/{slug}/{id}/activity",
        operation_id="record_activity_list",
        response_model=ActivityPage,
        response_model_exclude_unset=True,
        responses={
            status: {"content": {"application/problem+json": {"schema": problem}}}
            for status in (401, 403, 404, 422)
        },
        tags=["activity"],
    )
    async def timeline(request: Request, slug: str, id: str, ctx: Context = dependency) -> Any:
        builder = next(
            (
                item.builder
                for item in app._registrations.values()
                if (item.builder.api.prefix or item.slug) == slug
            ),
            None,
        )
        if builder is None or not builder.api.enabled or "retrieve" not in builder.api.operations:
            raise NotFound("Activity is unavailable.")
        params = {}
        for key, value in request.query_params.multi_items():
            if key not in {"kind", "limit", "cursor"}:
                raise invalid(key, "Unknown query parameter.", "unknown_parameter")
            if key in params:
                raise invalid(key, "Supply this parameter once.")
            params[key] = value
        return await read(activity, builder, id, params, ctx)

    return router
