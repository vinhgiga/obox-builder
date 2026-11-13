"""JSON adapter for the read-only activity plugin."""

from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.routing import APIRoute
from pydantic.alias_generators import to_camel

from obox_builder.builder.api_schema import FieldError, Problem
from obox_builder.builder.http import ApiRoute
from obox_builder.builder.query import invalid, parameters
from obox_builder.context import Context
from obox_builder.contrib.activity import Activity
from obox_builder.contrib.activity.schemas import (
    ActivityPage,
    CommentInput,
    CommentPage,
    CommentRead,
)
from obox_builder.contrib.activity.services import comment, comment_etag, comment_row, read
from obox_builder.errors import NotFound


class ActivityRoute(ApiRoute):
    cache_control = "private, no-store"


def build_router(activity: Activity, app_label: str | None = None) -> APIRouter:
    app = activity.app
    prefix = app.settings.api_prefix + (f"/{app_label}" if app_label else "")
    router = APIRouter(prefix=prefix, route_class=ActivityRoute)
    dependency = Depends(app.context_dependency("api"), scope="function")
    problem = Problem.model_json_schema()
    problem["properties"]["errors"]["items"] = FieldError.model_json_schema()
    problem.pop("$defs")
    router.responses.update(
        {
            status: {"content": {"application/problem+json": {"schema": problem}}}
            for status in (400, 401, 403, 404, 406, 409, 412, 415, 422, 428, 500)
        }
    )

    def resource(slug: str) -> Any:
        builder = next(
            (
                item.builder
                for item in app._registrations.values()
                if (item.builder.api.prefix or item.slug) == slug and item.app_label == app_label
            ),
            None,
        )
        if builder is None or not builder.api.enabled or "retrieve" not in builder.api.operations:
            raise NotFound("Activity is unavailable.")
        return builder

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
        builder = resource(slug)
        params = parameters(request.query_params.multi_items(), {"kind", "limit", "cursor"})
        page = await read(activity, builder, id, params, ctx)
        for item in page["items"]:
            for change in item.get("changes", ()):
                field = change["field"]
                if field in builder.api_identifiers:
                    for side in ("before", "after"):
                        if change.get(side) is not None:
                            change[side] = str(change[side])
                change["field"] = builder.api_names.get(field, to_camel(field))
        return page

    @router.get(
        "/{slug}/{id}/comments", response_model=CommentPage, operation_id="record_comments_list"
    )
    async def comments(request: Request, slug: str, id: str, ctx: Context = dependency) -> Any:
        params = parameters(request.query_params.multi_items(), {"limit", "cursor"})
        page = await read(activity, resource(slug), id, {**params, "kind": "comment"}, ctx)
        return {"items": [item["comment"] for item in page["items"]], "page": page["page"]}

    async def result(slug: str, id: str, ctx: Context, response: Response, **kwargs: Any) -> Any:
        obj = await comment(activity, resource(slug), id, ctx, **kwargs)
        row = await comment_row(activity, obj, ctx)
        response.headers["ETag"] = comment_etag(obj, row)
        return row

    @router.get(
        "/{slug}/{id}/comments/{comment_id}",
        response_model=CommentRead,
        operation_id="record_comment_retrieve",
    )
    async def retrieve(
        request: Request,
        slug: str,
        id: str,
        comment_id: str,
        response: Response,
        ctx: Context = dependency,
    ) -> Any:
        parameters(request.query_params.multi_items())
        return await result(slug, id, ctx, response, comment_id=comment_id)

    @router.post(
        "/{slug}/{id}/comments",
        response_model=CommentRead,
        status_code=201,
        operation_id="record_comment_create",
    )
    async def create(
        request: Request,
        slug: str,
        id: str,
        data: CommentInput,
        response: Response,
        ctx: Context = dependency,
    ) -> Any:
        app.csrf.validate(request, ctx.principal)
        for name in ("If-Match", "If-None-Match", "If-Unmodified-Since"):
            if name in request.headers:
                raise invalid(name, "Creation takes no precondition.", "unsupported_precondition")
        row = await result(slug, id, ctx, response, action="create", body=data.body)
        response.headers["Location"] = (
            request.scope.get("root_path", "") + request.url.path + "/" + row["id"]
        )
        return row

    @router.patch(
        "/{slug}/{id}/comments/{comment_id}",
        response_model=CommentRead,
        operation_id="record_comment_update",
    )
    async def update(
        request: Request,
        slug: str,
        id: str,
        comment_id: str,
        data: CommentInput,
        response: Response,
        ctx: Context = dependency,
    ) -> Any:
        app.csrf.validate(request, ctx.principal)
        return await result(
            slug,
            id,
            ctx,
            response,
            comment_id=comment_id,
            action="update",
            body=data.body,
            conditions=request.headers.getlist("if-match"),
        )

    @router.delete(
        "/{slug}/{id}/comments/{comment_id}", status_code=204, operation_id="record_comment_delete"
    )
    async def delete(
        request: Request, slug: str, id: str, comment_id: str, ctx: Context = dependency
    ) -> Response:
        app.csrf.validate(request, ctx.principal)
        if await request.body():
            raise invalid("body", "Request body must be empty.")
        await comment(
            activity,
            resource(slug),
            id,
            ctx,
            comment_id=comment_id,
            action="delete",
            conditions=request.headers.getlist("if-match"),
        )
        return Response(status_code=204)

    if app_label:
        for route in router.routes:
            if isinstance(route, APIRoute):
                route.name = f"{app_label}.{route.name}"
                route.operation_id = f"{app_label}.{route.operation_id}"
    return router
