"""REST adapter: static schemas and HTTP mapping over the shared service."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, Response

from obox_builder.context import Context
from obox_builder.errors import BuilderValidationError


def query_options(request: Request, builder: Any, *, pagination: bool) -> dict[str, Any]:
    query = request.query_params
    permitted = {"search", "ordering"}
    if pagination:
        permitted.update(("offset", "limit"))
    filters = {}
    for key in query:
        if key.startswith("filter."):
            filters[key[7:]] = query[key]
        elif key not in permitted:
            raise BuilderValidationError({key: ["Unknown query parameter."]})
        if len(query.getlist(key)) > 1:
            raise BuilderValidationError({key: ["Supply this parameter once."]})
    result: dict[str, Any] = {"filters": filters, "search": query.get("search")}
    if query.get("ordering"):
        result["ordering"] = tuple(query["ordering"].split(","))
    for name in ("offset", "limit"):
        if name in query:
            try:
                result[name] = int(query[name])
            except ValueError as exc:
                raise BuilderValidationError({name: ["Use a nonnegative integer."]}) from exc
    return result


def build_api(app: Any, builder: Any, service: Any) -> APIRouter:
    router = APIRouter()
    if not builder.api.enabled:
        return router
    path = f"{app.api_prefix}/{builder.api.prefix or builder.slug}"
    dependency = Depends(app.context_dependency("api"), scope="function")

    async def list_records(request: Request, ctx: Context = dependency) -> Any:
        objects = await service.list(
            ctx=ctx, **query_options(request, builder, pagination=builder.api.pagination)
        )
        return [await service.serialize(obj, ctx=ctx, fields=builder.api_read) for obj in objects]

    async def retrieve(id: str, ctx: Context = dependency) -> Any:
        return await service.serialize(
            await service.get(id, ctx=ctx), ctx=ctx, fields=builder.api_read
        )

    async def create(request: Request, data: Any, ctx: Context = dependency) -> Any:
        app.csrf.validate(request, ctx.principal)
        obj = await service.create(data.model_dump(exclude_unset=True), ctx=ctx)
        return await service.serialize(obj, ctx=ctx, fields=builder.api_read)

    async def update(request: Request, id: str, data: Any, ctx: Context = dependency) -> Any:
        app.csrf.validate(request, ctx.principal)
        obj = await service.update(id, data.model_dump(exclude_unset=True), ctx=ctx)
        return await service.serialize(obj, ctx=ctx, fields=builder.api_read)

    async def delete(request: Request, id: str, ctx: Context = dependency) -> Response:
        app.csrf.validate(request, ctx.principal)
        await service.delete(id, ctx=ctx)
        return Response(status_code=204)

    create.__annotations__["data"] = builder.create_schema
    update.__annotations__["data"] = builder.update_schema
    for action, endpoint, method, suffix, code in (
        ("list", list_records, "GET", "", 200),
        ("create", create, "POST", "", 201),
        ("retrieve", retrieve, "GET", "/{id}", 200),
        ("update", update, "PATCH", "/{id}", 200),
        ("delete", delete, "DELETE", "/{id}", 204),
    ):
        if action not in builder.api.actions:
            continue
        router.add_api_route(
            path + suffix,
            endpoint,
            methods=[method],
            status_code=code,
            name=f"obox_api_{builder.slug}_{action}",
            response_model=None
            if action == "delete"
            else (list[builder.read_schema] if action == "list" else builder.read_schema),
            response_model_exclude_unset=True,
            tags=list(builder.api.tags or (builder.model.__name__,)),
            openapi_extra={"x-obox-conditional-fields": True},
        )
    return router
