"""REST adapter: static schemas and HTTP mapping over the shared service."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast
from urllib.parse import quote

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field, StrictStr, TypeAdapter, create_model, field_serializer
from pydantic.alias_generators import to_camel

from obox_builder.builder.api_schema import ApiRequest, ApiResponse
from obox_builder.builder.filters import FilterGroup, FilterTerm, parse_filter
from obox_builder.builder.http import ApiRoute
from obox_builder.builder.query import ListQuery, invalid, parse_terms
from obox_builder.builder.service import invoke
from obox_builder.context import Context
from obox_builder.errors import (
    BuilderValidationError,
    ImproperlyConfigured,
    PermissionDenied,
    PreconditionFailed,
    PreconditionRequired,
)


class ActionInput(ApiRequest):
    record_ids: list[StrictStr] = Field(min_length=1)


class ActionResult(ApiResponse):
    count: int = Field(ge=0)


class QueryInput(ApiRequest):
    query: list[tuple[StrictStr, StrictStr]]


class Page(ApiResponse):
    limit: int | None = Field(ge=0)
    offset: int = Field(ge=0)
    has_more: bool
    next_cursor: str | None
    total: int | None = Field(ge=0)


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


class RecordOption(ApiResponse):
    id: str
    label: str


class OptionsPage(ApiResponse):
    items: list[RecordOption]
    page: Page


def list_schema(builder: Any, *, expanded: bool = True) -> type[BaseModel]:
    serializers = {}
    for name, spec in builder.specs.items():
        if name not in builder.read_schema.model_fields:
            continue
        if name in builder.api_identifiers:
            continue
        if isinstance(spec.column.type, sa.BigInteger):

            def convert(value: Any) -> Any:
                return None if value is None else str(value)

            result: Any = str | None if spec.column.nullable else str
        elif isinstance(spec.column.type, sa.Numeric):

            def convert(value: Any) -> Any:
                return None if value is None else str(value)

            result = str | None if spec.column.nullable else str
        elif isinstance(spec.column.type, sa.Integer | sa.DateTime):
            convert = json_value
            if isinstance(spec.column.type, sa.Integer):
                result = int | str | None if spec.column.nullable else int | str
            else:
                result = str | None if spec.column.nullable else str
        else:
            continue
        serializers["encode_" + name] = field_serializer(name, return_type=result)(convert)
    if any(field.alias == "expanded" for field in builder.read_schema.model_fields.values()):
        raise ImproperlyConfigured("The API field alias 'expanded' is reserved.")
    definitions: dict[str, Any] = {}
    if expanded:
        relationships: dict[str, Any] = {
            name: (RecordOption | None, None)
            for name, fk in builder.list_relation_fields.items()
            if fk in builder.api_read
        }
        aliases = [to_camel(name) for name in relationships]
        if len(set(aliases)) != len(aliases):
            raise ImproperlyConfigured("Relationship aliases must be unique.")
        expanded_schema = create_model(
            builder.model.__name__ + "Expanded", __base__=ApiResponse, **relationships
        )
        definitions["expanded"] = (expanded_schema, Field(default_factory=expanded_schema))
    for name, spec in builder.specs.items():
        if name not in builder.read_schema.model_fields:
            continue
        if isinstance(spec.column.type, sa.BigInteger):
            definitions[name] = (
                str | None if spec.column.nullable else str,
                None,
            )
    definitions[builder.pk_name] = (str, Field(alias="id"))
    return create_model(
        builder.model.__name__ + ("ListItem" if expanded else "Response"),
        __base__=builder.read_schema,
        __validators__=serializers,
        **definitions,
    )


def api_query(builder: Any, pairs: Iterable[tuple[str, str]]) -> ListQuery:
    """Resolve public field references once, without transforming query values."""
    params, filters = parse_terms(pairs)
    names = {alias: name for name, alias in builder.api_names.items()}

    def resolve(alias: str) -> str:
        if alias not in names:
            raise invalid(alias, "Unknown API field.", "unknown_field")
        return cast(str, names[alias])

    def tree(node: FilterGroup | FilterTerm) -> FilterGroup | FilterTerm:
        if isinstance(node, FilterTerm):
            return replace(node, field=resolve(node.field))
        return replace(node, children=tuple(tree(child) for child in node.children))

    if "select" in params and params["select"] != "*":
        params["select"] = ",".join(resolve(name) for name in params["select"].split(","))
    if "order" in params:
        params["order"] = ",".join(
            ".".join((resolve(parts[0]), *parts[1:]))
            for term in params["order"].split(",")
            for parts in (term.split("."),)
        )
    if "expand" in params:
        relations = {to_camel(name): name for name in builder.list_relations}
        selected = params["expand"].split(",")
        for name in selected:
            if "." in name:
                raise invalid("expand", "Only one-level expansion is supported.", "expand_depth")
            if name not in relations:
                raise invalid("expand", "Relationship target is unavailable.", "expand_unavailable")
        params["expand"] = ",".join(relations[name] for name in selected)
    return ListQuery(params, replace(filters, children=tuple(tree(n) for n in filters.children)))


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
    router = APIRouter(route_class=ApiRoute)
    if not builder.api.enabled:
        return router
    path = f"{app.settings.api_prefix}/{builder.api.prefix or builder.slug}"
    dependency = Depends(app.context_dependency("api"), scope="function")
    list_item_schema: Any = list_schema(builder)
    record_schema: Any = list_schema(builder, expanded=False)
    page_fields: dict[str, Any] = {
        "items": (list[list_item_schema], ...),
        "page": (Page, ...),
    }
    page_schema = create_model(builder.model.__name__ + "Page", __base__=ApiResponse, **page_fields)
    problems: dict[int | str, dict[str, Any]] = {
        status: {
            "content": {
                "application/problem+json": {"schema": {"$ref": "#/components/schemas/Problem"}}
            },
        }
        for status in (400, 401, 403, 404, 405, 406, 409, 412, 415, 422, 428, 500)
    }

    async def record_row(
        obj: Any, ctx: Context, fields: Sequence[str] | None = None
    ) -> dict[str, Any]:
        row = await service.serialize(
            obj, ctx=ctx, fields=builder.api_read if fields is None else fields
        )
        if builder.pk_name not in row:
            raise PermissionDenied("The primary key is not readable.")
        for name in builder.api_identifiers.intersection(row):
            if row[name] is not None:
                row[name] = str(row[name])
        return cast(dict[str, Any], row)

    async def etag(obj: Any, ctx: Context, row: dict[str, Any] | None = None) -> str:
        row = await record_row(obj, ctx) if row is None else row
        data = record_schema.model_validate(row).model_dump_json(exclude_unset=True)
        version = getattr(
            obj, sa.inspect(builder.model).get_property_by_column(builder.version_column).key
        )
        digest = hashlib.sha256(
            (builder.slug + ":" + str(json_value(version)) + ":" + data).encode()
        ).hexdigest()
        return '"' + digest + '"'

    async def record_response(obj: Any, ctx: Context, response: Response) -> dict[str, Any]:
        row = await record_row(obj, ctx)
        if builder.version_column is not None:
            response.headers["ETag"] = await etag(obj, ctx, row)
        return row

    def precondition(request: Request, ctx: Context) -> Callable[[Any], Awaitable[None]] | None:
        if builder.version_column is None and "if-match" not in request.headers:
            return None

        async def check(obj: Any) -> None:
            values = request.headers.getlist("if-match")
            if builder.version_column is None:
                if values:
                    raise invalid(
                        "If-Match", "This resource is not versioned.", "unsupported_precondition"
                    )
                return
            if not values:
                raise PreconditionRequired("Send the record's ETag in If-Match.")
            value = ",".join(values).strip()
            if value == "*":
                return
            tag = r'(?:W/)?"[\x21\x23-\x7e\x80-\xff]*"'
            element = r"\s*(?:" + tag + r")?\s*"
            if not re.fullmatch(element + r"(?:," + element + r")*", value):
                raise invalid("If-Match", "Use an entity-tag list or *.", "invalid_precondition")
            if await etag(obj, ctx) not in re.findall(tag, value):
                raise PreconditionFailed("The record has changed.")

        return check

    def query_parameters(*, filters: bool = True, projection: bool = True) -> list[dict[str, Any]]:
        limit_schema: dict[str, Any] = {"type": "integer", "minimum": 0}
        if builder.api.max_limit is not None:
            limit_schema["maximum"] = builder.api.max_limit
        if builder.api.default_limit is not None:
            limit_schema["default"] = builder.api.default_limit
        parameters = [
            {"name": "search", "in": "query", "required": False, "schema": {"type": "string"}},
            {"name": "order", "in": "query", "required": False, "schema": {"type": "string"}},
            {
                "name": "limit",
                "in": "query",
                "required": False,
                "schema": limit_schema,
            },
            {
                "name": "offset",
                "in": "query",
                "required": False,
                "schema": {"type": "integer", "minimum": 0},
            },
            {"name": "cursor", "in": "query", "required": False, "schema": {"type": "string"}},
            {
                "name": "count",
                "in": "query",
                "required": False,
                "schema": {"type": "string", "enum": ["exact"]},
            },
        ]
        if projection:
            parameters[1:1] = [
                {"name": "select", "in": "query", "required": False, "schema": {"type": "string"}},
                {"name": "expand", "in": "query", "required": False, "schema": {"type": "string"}},
            ]
        if filters:
            parameters.extend(
                {
                    "name": builder.api_names[name],
                    "in": "query",
                    "required": False,
                    "schema": {"type": "string"},
                    "description": "Filter expression using field.operator.value.",
                }
                for name in builder.filter_fields
            )
            parameters.extend(
                {
                    "name": name,
                    "in": "query",
                    "required": False,
                    "schema": {"type": "string"},
                    "description": "Parenthesized logical filter group.",
                }
                for name in ("and", "or", "not.and", "not.or")
            )
        return parameters

    async def list_page(pairs: Iterable[tuple[str, str]], ctx: Context) -> Any:
        await service.authorize("list", ctx)
        query = api_query(builder, pairs)
        params = query.params
        selected = params.get("select", "*")
        fields = list(builder.api_read) if selected == "*" else selected.split(",")
        if any(name not in builder.api_read for name in fields):
            raise invalid("select", "Unknown API field.", "unknown_field")
        if builder.pk_name not in fields:
            fields.append(builder.pk_name)
        objects, page = await service.page(query, ctx=ctx)
        rows = [await record_row(obj, ctx, fields) for obj in objects]
        if "expand" in params:
            await service.expand(objects, rows, params["expand"].split(","), ctx=ctx)
        return {"items": rows, "page": page} if builder.api.list_envelope else rows

    async def list_records(request: Request, ctx: Context = dependency) -> Any:
        request.state.api_names = builder.api_names
        return await list_page(request.query_params.multi_items(), ctx)

    async def query_records(request: Request, data: QueryInput, ctx: Context = dependency) -> Any:
        request.state.api_names = builder.api_names
        return await list_page(data.query, ctx)

    if {"list", "retrieve"}.issubset(builder.api.operations):

        @router.get(
            path + ":queryOptions",
            name=f"obox_api_{builder.slug}_query_options",
            operation_id=f"{builder.slug}_query_options",
            response_model=OptionsPage,
            responses=problems,
            tags=list(builder.api.tags or (builder.model.__name__,)),
            openapi_extra={"parameters": query_parameters(projection=False)},
        )
        async def list_options(request: Request, ctx: Context = dependency) -> Any:
            request.state.api_names = builder.api_names
            await service.authorize("list", ctx)
            await service.authorize("retrieve", ctx)
            pk, label = builder.pk_name, builder.label_field
            if pk not in builder.readable(ctx, fields=builder.api_read):
                raise PermissionDenied("The primary key is not readable.")
            query = api_query(builder, request.query_params.multi_items())
            params = query.params
            for name in ("select", "expand"):
                if name in params:
                    raise invalid(name, "Unknown query parameter.", "unknown_parameter")
            objects, page = await service.page(query, ctx=ctx, retrieve=True)
            fields = [name for name in (pk, label) if name in builder.api_read]
            items = []
            for obj in objects:
                if not await invoke(builder.policy.allow, "retrieve", ctx, obj=obj):
                    continue
                row = await service.serialize(obj, ctx=ctx, fields=fields)
                if pk not in row:
                    continue
                value = row.get(label)
                items.append(
                    {
                        "id": str(row[pk]),
                        "label": str(row[pk] if value is None or value == "" else value),
                    }
                )
            return {"items": items, "page": page}

    async def retrieve(id: str, response: Response, ctx: Context = dependency) -> Any:
        return await record_response(await service.get(id, ctx=ctx), ctx, response)

    async def create(
        request: Request, response: Response, data: Any, ctx: Context = dependency
    ) -> Any:
        request.state.record_fields = builder.create_schema.model_fields
        app.csrf.validate(request, ctx.principal)
        obj = await service.create(data.model_dump(by_alias=False, exclude_unset=True), ctx=ctx)
        row = await record_response(obj, ctx, response)
        response.headers["Location"] = (
            request.scope.get("root_path", "") + path + "/" + quote(row[builder.pk_name], safe="")
        )
        return row

    async def update(
        request: Request, response: Response, id: str, data: Any, ctx: Context = dependency
    ) -> Any:
        request.state.record_fields = builder.update_schema.model_fields
        app.csrf.validate(request, ctx.principal)
        obj = await service.update(
            id,
            data.model_dump(by_alias=False, exclude_unset=True),
            ctx=ctx,
            precondition=precondition(request, ctx),
        )
        return await record_response(obj, ctx, response)

    async def delete(request: Request, id: str, ctx: Context = dependency) -> Response:
        request.state.record_fields = builder.read_schema.model_fields
        app.csrf.validate(request, ctx.principal)
        if await request.body():
            raise BuilderValidationError({"__all__": ["Request body must be empty."]})
        await service.delete(id, ctx=ctx, precondition=precondition(request, ctx))
        return Response(status_code=204)

    create.__annotations__["data"] = builder.create_schema
    update.__annotations__["data"] = builder.update_schema
    for operation, endpoint, method, suffix, code in (
        ("list", list_records, "GET", "", 200),
        ("list", query_records, "POST", ":query", 200),
        ("create", create, "POST", "", 201),
        ("retrieve", retrieve, "GET", "/{id}", 200),
        ("update", update, "PATCH", "/{id}", 200),
        ("delete", delete, "DELETE", "/{id}", 204),
    ):
        if operation not in builder.api.operations:
            continue
        extra: dict[str, Any] = {"x-obox-conditional-fields": True}
        if method == "GET" and suffix == "":
            extra["parameters"] = query_parameters()
        elif method in {"PATCH", "DELETE"}:
            extra["parameters"] = [
                {
                    "name": "If-Match",
                    "in": "header",
                    "required": builder.version_column is not None,
                    "schema": {"type": "string"},
                }
            ]
        responses = dict(problems)
        if builder.version_column is not None and operation in {"retrieve", "create", "update"}:
            responses[code] = {"headers": {"ETag": {"schema": {"type": "string"}}}}
        router.add_api_route(
            path + suffix,
            endpoint,
            methods=[method],
            status_code=code,
            operation_id=f"{builder.slug}_{'query' if endpoint is query_records else operation}",
            name=f"obox_api_{builder.slug}_{'query' if endpoint is query_records else operation}",
            response_model=None
            if operation == "delete"
            else (
                (page_schema if builder.api.list_envelope else list[list_item_schema])
                if operation == "list"
                else record_schema
            ),
            response_model_exclude_unset=True,
            tags=list(builder.api.tags or (builder.model.__name__,)),
            openapi_extra=extra,
            responses=responses,
        )

    def action_endpoint(name: str) -> Any:
        async def run_action(request: Request, data: ActionInput, ctx: Context = dependency) -> Any:
            app.csrf.validate(request, ctx.principal)
            return {"count": await service.run_action(name, data.record_ids, ctx=ctx)}

        return run_action

    for name in builder.actions:
        router.add_api_route(
            path + ":" + to_camel(name),
            action_endpoint(name),
            methods=["POST"],
            operation_id=f"{builder.slug}_action_{to_camel(name)}",
            name=f"obox_api_{builder.slug}_action_{name}",
            response_model=ActionResult,
            responses=problems,
            tags=list(builder.api.tags or (builder.model.__name__,)),
        )
    return router
