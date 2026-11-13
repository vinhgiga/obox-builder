"""REST adapter: static schemas and HTTP mapping over the shared service."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast
from urllib.parse import quote

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field, StrictStr, TypeAdapter, create_model, field_serializer
from pydantic.alias_generators import to_camel

from obox_builder.builder.api_schema import ApiRequest, ApiResponse
from obox_builder.builder.conditional import check_match
from obox_builder.builder.fields import schema, validate
from obox_builder.builder.filters import FilterGroup, FilterTerm, parse_filter
from obox_builder.builder.http import ApiRoute
from obox_builder.builder.query import ListQuery, invalid, parameters, parse_terms
from obox_builder.builder.service import BatchOperation, UpsertResult, invoke
from obox_builder.context import Context
from obox_builder.errors import (
    BatchOperationError,
    BuilderValidationError,
    ImproperlyConfigured,
    NotFound,
    OboxError,
    PermissionDenied,
)


class ActionInput(ApiRequest):
    record_ids: list[StrictStr] = Field(min_length=1)


class ActionResult(ApiResponse):
    count: int = Field(ge=0)


class QueryInput(ApiRequest):
    query: list[tuple[StrictStr, StrictStr]]


class RelationshipInput(ApiRequest):
    record_ids: list[StrictStr]
    if_matches: dict[str, StrictStr] = Field(default_factory=dict)


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


def normalize_datetime(value: datetime) -> datetime:
    return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC)


def normalize_optional_datetime(value: datetime | None) -> datetime | None:
    return None if value is None else normalize_datetime(value)


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
        elif isinstance(spec.column.type, sa.Float):
            continue
        elif isinstance(spec.column.type, sa.Numeric):

            def convert(value: Any) -> Any:
                return None if value is None else str(value)

            result = str | None if spec.column.nullable else str
        elif isinstance(spec.column.type, sa.Integer | sa.DateTime):
            if isinstance(spec.column.type, sa.Integer):
                convert = json_value
                result = int | str | None if spec.column.nullable else int | str
            elif spec.column.nullable:
                convert = normalize_optional_datetime
                result = datetime | None
            else:
                convert = normalize_datetime
                result = datetime
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
            builder.schema_name + "Expanded", __base__=ApiResponse, **relationships
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
        builder.schema_name + ("ListItem" if expanded else "Response"),
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


async def record_etag(builder: Any, service: Any, obj: Any, ctx: Context) -> str:
    ctx = replace(ctx, source="api")
    row = await service.serialize(obj, ctx=ctx, fields=builder.api_read)
    for name in builder.api_identifiers.intersection(row):
        if row[name] is not None:
            row[name] = str(row[name])
    data = (
        list_schema(builder, expanded=False).model_validate(row).model_dump_json(exclude_unset=True)
    )
    version = getattr(
        obj, sa.inspect(builder.model).get_property_by_column(builder.version_column).key
    )
    digest = hashlib.sha256(
        (builder.route_key + ":" + str(json_value(version)) + ":" + data).encode()
    ).hexdigest()
    return '"' + digest + '"'


def build_api(app: Any, builder: Any, service: Any) -> APIRouter:
    from obox_builder.builder.describe import ResourceDescription, ResourceDescriptions, describe
    from obox_builder.builder.records import RelationshipPage, relationship

    router = APIRouter(route_class=ApiRoute)
    if not builder.api.enabled:
        return router
    path = builder.api_path
    dependency = Depends(app.context_dependency("api"), scope="function")
    list_item_schema: Any = list_schema(builder)
    record_schema: Any = list_schema(builder, expanded=False)
    page_fields: dict[str, Any] = {
        "items": (list[list_item_schema], ...),
        "page": (Page, ...),
    }
    page_schema = create_model(builder.schema_name + "Page", __base__=ApiResponse, **page_fields)
    problems: dict[int | str, dict[str, Any]] = {
        status: {
            "content": {
                "application/problem+json": {"schema": {"$ref": "#/components/schemas/Problem"}}
            },
        }
        for status in (400, 401, 403, 404, 405, 406, 409, 412, 415, 422, 428, 500)
    }

    @router.get(
        path + ":describe",
        response_model=ResourceDescription,
        name=f"obox_api_{builder.route_key}_describe",
        operation_id=f"{builder.route_key}_describe",
        responses=problems,
    )
    async def describe_resource(
        request: Request, response: Response, ctx: Context = dependency
    ) -> Any:
        parameters(request.query_params.multi_items())
        response.headers["Cache-Control"] = "private, no-store"
        return await describe(app, builder, ctx, record_schema)

    if not any(item.builder.api.enabled for item in app._registrations.values()):

        @router.get(
            app.settings.api_prefix + ":describe",
            response_model=ResourceDescriptions,
            name="obox_api_describe",
            operation_id="obox_describe",
            responses=problems,
        )
        async def describe_resources(
            request: Request, response: Response, ctx: Context = dependency
        ) -> Any:
            from obox_builder.errors import NotFound

            parameters(request.query_params.multi_items())
            response.headers["Cache-Control"] = "private, no-store"
            resources = []
            for item in app._registrations.values():
                if not item.builder.api.enabled:
                    continue
                try:
                    resources.append(
                        await describe(
                            app, item.builder, ctx, list_schema(item.builder, expanded=False)
                        )
                    )
                except NotFound:
                    continue
            return {"resources": resources}

    if "retrieve" in builder.api.operations and (
        app.get_plugin("files") is not None or app.get_plugin("activity") is not None
    ):
        from obox_builder.builder.metadata import MetadataQuery, MetadataResults, query_metadata

        @router.post(
            path + ":queryMetadata",
            response_model=MetadataResults,
            response_model_exclude_unset=True,
            name=f"obox_api_{builder.route_key}_query_metadata",
            operation_id=f"{builder.route_key}_query_metadata",
            responses=problems,
        )
        async def metadata(
            request: Request, response: Response, data: MetadataQuery, ctx: Context = dependency
        ) -> Any:
            parameters(request.query_params.multi_items())
            response.headers["Cache-Control"] = "private, no-store"
            return await query_metadata(app, builder, data, ctx)

        metadata.__annotations__["data"] = MetadataQuery

    @router.options(
        path,
        response_model=ResourceDescription,
        name=f"obox_api_{builder.route_key}_options",
        responses=problems,
    )
    @router.options(
        path + "/{id}",
        response_model=ResourceDescription,
        name=f"obox_api_{builder.route_key}_detail_options",
        responses=problems,
    )
    async def options_resource(
        request: Request, response: Response, ctx: Context = dependency
    ) -> Any:
        from obox_builder.builder.http import allow_header

        response.headers["Allow"] = allow_header(request.scope)
        response.headers["Cache-Control"] = "private, no-store"
        return await describe(app, builder, ctx, record_schema)

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
        return await record_etag(builder, service, obj, ctx)

    async def record_response(obj: Any, ctx: Context, response: Response) -> dict[str, Any]:
        row = await record_row(obj, ctx)
        if builder.version_column is not None:
            response.headers["ETag"] = await etag(obj, ctx, row)
        return row

    async def receipt_start(data: BaseModel, ctx: Context) -> tuple[Any, bool]:
        plugin = app.get_plugin("idempotency")
        if plugin is None:
            if ctx.request is not None and "idempotency-key" in ctx.request.headers:
                raise invalid(
                    "Idempotency-Key", "Enable the Idempotency plugin to use this header."
                )
            return None, False
        return cast(
            tuple[Any, bool],
            await plugin.begin(
                data.model_dump(mode="json", by_alias=True, exclude_unset=True), ctx
            ),
        )

    async def receipt_replay(receipt: Any, ctx: Context, response: Response) -> Any:
        saved = receipt.response
        for proof in saved["proofs"]:
            await service.authorize(proof["action"], ctx)
            if proof["action"] == "delete":
                continue
            identity = validate(
                schema("ReceiptIdentity", builder.specs, (builder.pk_name,)),
                {builder.pk_name: proof["id"]},
            )[builder.pk_name]
            action = "update" if proof["action"] == "update" else "retrieve"
            obj = (
                (
                    await ctx.session.scalars(
                        service._statement(action, ctx).where(builder.pk == identity)
                    )
                )
                .unique()
                .one_or_none()
            )
            if obj is None:
                raise NotFound("The saved response is no longer available.")
            await service.authorize(proof["action"], ctx, obj)
            readable = set(builder.readable(ctx, obj=obj)).intersection(builder.api_read)
            if not set(proof["fields"]).issubset(readable):
                raise PermissionDenied("The saved response is no longer readable.")
        response.headers.update(saved["headers"])
        return saved["body"]

    def receipt_finish(
        receipt: Any,
        schema: type[BaseModel],
        body: Any,
        response: Response,
        proofs: list[dict[str, Any]],
    ) -> None:
        if receipt is not None:
            receipt.response = {
                "body": schema.model_validate(body).model_dump(
                    mode="json", by_alias=False, exclude_unset=True
                ),
                "headers": {
                    key: value
                    for key, value in response.headers.items()
                    if key.lower() in {"etag", "location"}
                },
                "proofs": proofs,
            }

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
            check_match(values, await etag(obj, ctx))

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
            name=f"obox_api_{builder.route_key}_query_options",
            operation_id=f"{builder.route_key}_query_options",
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

    async def retrieve(
        request: Request, id: str, response: Response, ctx: Context = dependency
    ) -> Any:
        parameters(request.query_params.multi_items())
        return await record_response(await service.get(id, ctx=ctx), ctx, response)

    if set(builder.api.operations).intersection({"create", "update", "delete"}):
        operation_schemas = []
        opcodes: list[tuple[str, Any]] = [
            ("create", Literal["create"]),
            ("update", Literal["update"]),
            ("delete", Literal["delete"]),
        ]
        if builder.api.upsert_fields:
            opcodes.append(("upsert", Literal["upsert"]))
        for name, opcode in opcodes:
            definitions: dict[str, Any] = {"op": (opcode, ...)}
            if name not in {"create", "upsert"}:
                definitions.update(id=(StrictStr, ...), if_match=(StrictStr | None, None))
            if name == "upsert":
                definitions.update(
                    merge_on=(list[StrictStr], Field(min_length=1)),
                    if_match=(StrictStr | None, None),
                )
            if name != "delete":
                definitions["data"] = (
                    getattr(builder, ("update" if name == "upsert" else name) + "_schema"),
                    ...,
                )
            operation_schemas.append(
                create_model(
                    builder.schema_name + name.title() + "Operation",
                    __base__=ApiRequest,
                    **definitions,
                )
            )
        operation_type: Any = operation_schemas[0] | operation_schemas[1] | operation_schemas[2]
        for extra_schema in operation_schemas[3:]:
            operation_type = operation_type | extra_schema
        operation_union: Any = Annotated[operation_type, Field(discriminator="op")]
        batch_fields: dict[str, Any] = {
            "operations": (
                list[operation_union],
                Field(min_length=1, max_length=builder.api.max_batch),
            )
        }
        batch_schema = create_model(
            builder.schema_name + "BatchWrite",
            __base__=ApiRequest,
            **batch_fields,
        )
        result_fields: dict[str, Any] = {
            "op": (Literal["create", "update", "delete", "upsert"], ...),
            "id": (str, ...),
            "record": (record_schema | None, None),
            "etag": (str | None, None),
        }
        if builder.api.upsert_fields:
            result_fields["created"] = (bool | None, None)
        result_schema: Any = create_model(
            builder.schema_name + "BatchResult",
            __base__=ApiResponse,
            **result_fields,
        )
        results_fields: dict[str, Any] = {"results": (list[result_schema], ...)}
        results_schema = create_model(
            builder.schema_name + "BatchResults", __base__=ApiResponse, **results_fields
        )

        async def batch_write(
            request: Request, response: Response, data: Any, ctx: Context = dependency
        ) -> Any:
            app.csrf.validate(request, ctx.principal)
            for header in ("If-Match", "If-None-Match", "If-Unmodified-Since"):
                if header in request.headers:
                    raise invalid(
                        header, "Use per-operation conditions.", "unsupported_precondition"
                    )
            request.state.api_names = builder.api_names
            request.state.record_fields = builder.update_schema.model_fields
            operations = []
            for index, item in enumerate(data.operations):
                required = {"create", "update"} if item.op == "upsert" else {item.op}
                if not required.issubset(builder.api.operations):
                    raise BatchOperationError(index, PermissionDenied("Operation is not exposed."))
                try:
                    for action in sorted(required):
                        await service.authorize(action, ctx)
                except OboxError as exc:
                    raise BatchOperationError(index, exc) from exc
                aliases = {alias: name for name, alias in builder.api_names.items()}
                merge_on = getattr(item, "merge_on", ())
                if any(alias not in aliases for alias in merge_on):
                    raise BatchOperationError(
                        index, invalid("merge_on", "Use public API field names.", "unknown_field")
                    )

                async def check(
                    obj: Any, token: str | None = getattr(item, "if_match", None)
                ) -> None:
                    if builder.version_column is None:
                        if token is not None:
                            raise invalid(
                                "If-Match",
                                "This resource is not versioned.",
                                "unsupported_precondition",
                            )
                    else:
                        check_match([] if token is None else [token], await etag(obj, ctx))

                operations.append(
                    BatchOperation(
                        item.op,
                        getattr(item, "id", None),
                        item.data.model_dump(by_alias=False, exclude_unset=True)
                        if item.op != "delete"
                        else {},
                        check if item.op != "create" else None,
                        tuple(aliases[alias] for alias in merge_on),
                    )
                )
            receipt, replay = await receipt_start(data, ctx)
            if replay:
                return await receipt_replay(receipt, ctx, response)
            objects = await service.batch(operations, ctx=ctx)
            results = []
            proofs = []
            for index, (operation, obj) in enumerate(zip(operations, objects, strict=True)):
                try:
                    result = {"op": operation.op, "id": operation.id}
                    if isinstance(obj, UpsertResult):
                        result["created"] = obj.created
                        obj = obj.record
                    if obj is not None:
                        result["record"] = await record_row(obj, ctx)
                        result["id"] = result["record"][builder.pk_name]
                        if builder.version_column is not None:
                            result["etag"] = await etag(obj, ctx)
                    results.append(result)
                    proofs.append(
                        {
                            "action": ("create" if result.get("created") else "update")
                            if operation.op == "upsert"
                            else operation.op,
                            "id": result["id"],
                            "fields": list(result.get("record", {})),
                        }
                    )
                except OboxError as exc:
                    raise BatchOperationError(index, exc) from exc
            body = {"results": results}
            receipt_finish(receipt, results_schema, body, response, proofs)
            return body

        batch_write.__annotations__["data"] = batch_schema
        router.add_api_route(
            path + ":batchWrite",
            batch_write,
            methods=["POST"],
            name=f"obox_api_{builder.route_key}_batch_write",
            operation_id=f"{builder.route_key}_batch_write",
            response_model=results_schema,
            response_model_exclude_unset=True,
            responses=problems,
            tags=list(builder.api.tags or (builder.model.__name__,)),
            openapi_extra={
                "parameters": [
                    {
                        "name": "Idempotency-Key",
                        "in": "header",
                        "required": False,
                        "schema": {"type": "string", "minLength": 1, "maxLength": 255},
                    }
                ]
            }
            if app.get_plugin("idempotency")
            else None,
        )

    if "retrieve" in builder.api.operations:

        @router.get(
            path + "/{id}/relationships/{field}",
            name=f"obox_api_{builder.route_key}_relationships",
            response_model=RelationshipPage,
            operation_id=f"{builder.route_key}_relationships",
            responses=problems,
        )
        async def related(
            request: Request, id: str, field: str, response: Response, ctx: Context = dependency
        ) -> Any:
            response.headers["Cache-Control"] = "private, no-store"
            return await relationship(
                app,
                builder,
                id,
                field,
                parameters(request.query_params.multi_items(), {"limit", "cursor"}),
                ctx,
            )

    if builder.api.write_relationships and "update" in builder.api.operations:

        async def write_relationship(
            request: Request,
            response: Response,
            id: str,
            field: str,
            data: RelationshipInput,
            ctx: Context = dependency,
        ) -> Any:
            from obox_builder.contrib.relationships import mutate

            app.csrf.validate(request, ctx.principal)
            for header in ("If-None-Match", "If-Unmodified-Since"):
                if header in request.headers:
                    raise invalid(header, "Unsupported precondition.", "unsupported_precondition")
            names = {to_camel(name): name for name in builder.api.write_relationships}
            if field not in names:
                from obox_builder.errors import NotFound

                raise NotFound("Relationship is unavailable.")
            modes: dict[str, Any] = {"POST": "add", "PUT": "replace", "DELETE": "remove"}
            obj = await mutate(
                service,
                id,
                names[field],
                data.record_ids,
                data.if_matches,
                modes[request.method],
                ctx,
                precondition(request, ctx),
            )
            return await record_response(obj, ctx, response)

        for method in ("POST", "PUT", "DELETE"):
            router.add_api_route(
                path + "/{id}/relationships/{field}",
                write_relationship,
                methods=[method],
                name=f"obox_api_{builder.route_key}_relationship_{method.lower()}",
                operation_id=f"{builder.route_key}_relationship_{method.lower()}",
                response_model=record_schema,
                response_model_exclude_unset=True,
                responses=problems,
                openapi_extra={
                    "parameters": [
                        {
                            "name": "If-Match",
                            "in": "header",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ]
                }
                if builder.version_column is not None
                else None,
            )

    async def create(
        request: Request, response: Response, data: Any, ctx: Context = dependency
    ) -> Any:
        request.state.record_fields = builder.create_schema.model_fields
        app.csrf.validate(request, ctx.principal)
        await service.authorize("create", ctx)
        receipt, replay = await receipt_start(data, ctx)
        if replay:
            return await receipt_replay(receipt, ctx, response)
        obj = await service.create(data.model_dump(by_alias=False, exclude_unset=True), ctx=ctx)
        row = await record_response(obj, ctx, response)
        response.headers["Location"] = (
            request.scope.get("root_path", "") + path + "/" + quote(row[builder.pk_name], safe="")
        )
        receipt_finish(
            receipt,
            record_schema,
            row,
            response,
            [
                {
                    "action": "create",
                    "id": row[builder.pk_name],
                    "fields": list(row),
                }
            ],
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
        elif operation == "create" and app.get_plugin("idempotency"):
            extra["parameters"] = [
                {
                    "name": "Idempotency-Key",
                    "in": "header",
                    "required": False,
                    "schema": {"type": "string", "minLength": 1, "maxLength": 255},
                }
            ]
        elif method in {"PATCH", "DELETE"} and builder.version_column is not None:
            extra["parameters"] = [
                {
                    "name": "If-Match",
                    "in": "header",
                    "required": True,
                    "schema": {"type": "string"},
                }
            ]
        responses = dict(problems)
        if builder.version_column is not None and operation in {"retrieve", "create", "update"}:
            responses[code] = {"headers": {"ETag": {"schema": {"type": "string"}}}}
        endpoint_name = "query" if endpoint is query_records else operation
        router.add_api_route(
            path + suffix,
            endpoint,
            methods=[method],
            status_code=code,
            operation_id=f"{builder.route_key}_{endpoint_name}",
            name=f"obox_api_{builder.route_key}_{endpoint_name}",
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
            operation_id=f"{builder.route_key}_action_{to_camel(name)}",
            name=f"obox_api_{builder.route_key}_action_{name}",
            response_model=ActionResult,
            responses=problems,
            tags=list(builder.api.tags or (builder.model.__name__,)),
        )
    return router
