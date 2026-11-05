"""REST adapter: static schemas and HTTP mapping over the shared service."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, create_model, field_serializer

from obox_builder.builder.filters import parse_filter
from obox_builder.builder.http import ListRoute, OboxRoute
from obox_builder.builder.query import ListQuery, invalid, parse_terms
from obox_builder.context import Context
from obox_builder.errors import BuilderValidationError


class ActionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    record_ids: list[str | int] = Field(min_length=1)


class Page(BaseModel):
    limit: int | None
    offset: int
    has_more: bool
    next_cursor: str | None
    total: int | None


def json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return (
            value.replace(tzinfo=value.tzinfo or UTC)
            .astimezone(UTC)
            .isoformat()
            .replace("+00:00", "Z")
        )
    if isinstance(value, int) and abs(value) > 2**53 - 1:
        return str(value)
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [json_value(item) for item in value]
    return TypeAdapter[Any](Any).dump_python(value, mode="json")


def list_schema(builder: Any) -> type[BaseModel]:
    serializers = {}
    for name, spec in builder.specs.items():
        if name not in builder.read_schema.model_fields:
            continue
        if isinstance(spec.column.type, sa.BigInteger):

            def convert(value: Any) -> Any:
                return None if value is None else str(value)

            result: Any = str | None
        elif isinstance(spec.column.type, sa.Integer | sa.DateTime):
            convert = json_value
            result = int | str | None if isinstance(spec.column.type, sa.Integer) else str | None
        else:
            continue
        serializers["encode_" + name] = field_serializer(name, return_type=result)(convert)
    expansion = "expanded"
    while expansion in builder.read_schema.model_fields:
        expansion += "_"
    serializers["encode_expanded"] = field_serializer(expansion)(json_value)
    definitions: dict[str, Any] = {
        expansion: (dict[str, Any], Field(default_factory=dict, alias="_expanded"))
    }
    return create_model(
        builder.model.__name__ + "ListRead",
        __base__=builder.read_schema,
        __validators__=serializers,
        **definitions,
    )


def query_options(
    request: Request, builder: Any, *, pagination: bool, grouping: bool = False
) -> dict[str, Any]:
    query = request.query_params
    permitted = {"search", "ordering"}
    if pagination:
        permitted.update(("offset", "limit"))
    if grouping:
        permitted.add("group")
    filters = {}
    filter_ops = {}
    filter_group = None
    for key in query:
        if key == "filter":
            filter_group = parse_filter(query[key])
        elif key.startswith("filter."):
            filters[key[7:]] = query[key]
        elif key.startswith("filter_op."):
            filter_ops[key[10:]] = query[key]
        elif key not in permitted:
            raise BuilderValidationError({key: ["Unknown query parameter."]})
        if len(query.getlist(key)) > 1:
            raise BuilderValidationError({key: ["Supply this parameter once."]})
    result: dict[str, Any] = {
        "filters": filters,
        "filter_ops": filter_ops,
        "filter_group": filter_group,
        "search": query.get("search"),
    }
    if query.get("ordering"):
        result["ordering"] = tuple(query["ordering"].split(","))
    if grouping and query.get("group"):
        result["grouping"] = tuple(term for term in query["group"].split(",") if term)
    for name in ("offset", "limit"):
        if name in query:
            try:
                result[name] = int(query[name])
            except ValueError as exc:
                raise BuilderValidationError({name: ["Use a nonnegative integer."]}) from exc
    return result


def build_api(app: Any, builder: Any, service: Any) -> APIRouter:
    router = APIRouter(route_class=OboxRoute)
    if not builder.api.enabled:
        return router
    path = f"{app.settings.api_prefix}/{builder.api.prefix or builder.slug}"
    dependency = Depends(app.context_dependency("api"), scope="function")
    item_schema: Any = list_schema(builder)
    page_fields: dict[str, Any] = {"items": (list[item_schema], ...), "page": (Page, ...)}
    page_schema = create_model(builder.model.__name__ + "Page", **page_fields)

    async def list_records(request: Request, ctx: Context = dependency) -> Any:
        await service.authorize("list", ctx)
        params, filters = parse_terms(request.query_params.multi_items())
        query = ListQuery(params, filters)
        selected = params.get("select", "*")
        fields = list(builder.api_read) if selected == "*" else selected.split(",")
        if any(name not in builder.api_read for name in fields):
            raise invalid("select", "Unknown API field.", "unknown_field")
        if builder.pk_name in builder.api_read and builder.pk_name not in fields:
            fields.append(builder.pk_name)
        objects, page = await service.page(query, ctx=ctx)
        rows = [await service.serialize(obj, ctx=ctx, fields=fields) for obj in objects]
        if "expand" in params:
            await service.expand(objects, rows, params["expand"].split(","), ctx=ctx)
        return {"items": rows, "page": page} if builder.api.list_envelope else rows

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

    if builder.actions:

        @router.post(
            path + "/actions/{name}",
            name=f"obox_api_{builder.slug}_action",
            tags=list(builder.api.tags or (builder.model.__name__,)),
        )
        async def run_action(
            request: Request, name: str, data: ActionInput, ctx: Context = dependency
        ) -> dict[str, int]:
            app.csrf.validate(request, ctx.principal)
            return {"count": await service.run_action(name, data.record_ids, ctx=ctx)}

    create.__annotations__["data"] = builder.create_schema
    update.__annotations__["data"] = builder.update_schema
    for operation, endpoint, method, suffix, code in (
        ("list", list_records, "GET", "", 200),
        ("create", create, "POST", "", 201),
        ("retrieve", retrieve, "GET", "/{id}", 200),
        ("update", update, "PATCH", "/{id}", 200),
        ("delete", delete, "DELETE", "/{id}", 204),
    ):
        if operation not in builder.api.operations:
            continue
        router.add_api_route(
            path + suffix,
            endpoint,
            methods=[method],
            status_code=code,
            name=f"obox_api_{builder.slug}_{operation}",
            response_model=None
            if operation == "delete"
            else (
                (page_schema if builder.api.list_envelope else list[item_schema])
                if operation == "list"
                else builder.read_schema
            ),
            response_model_exclude_unset=True,
            tags=list(builder.api.tags or (builder.model.__name__,)),
            openapi_extra={"x-obox-conditional-fields": True},
            route_class_override=ListRoute if operation == "list" else OboxRoute,
        )
    return router
