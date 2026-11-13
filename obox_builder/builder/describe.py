"""Principal-filtered metadata for registered resource APIs."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import sqlalchemy as sa
from pydantic import Field, JsonValue
from pydantic.alias_generators import to_camel

from obox_builder.builder.api_schema import ApiResponse
from obox_builder.context import Context
from obox_builder.errors import NotFound


class FieldDescription(ApiResponse):
    name: str
    definition: dict[str, Any] = Field(alias="schema")
    nullable: bool
    readable: bool
    read_only: bool
    required: bool
    writable: dict[str, bool]
    reference: str | None = None
    kind: str
    options: dict[str, JsonValue]
    computed: bool


class ResourceDescription(ApiResponse):
    slug: str
    path: str
    operations: dict[str, bool]
    scopes: dict[str, str]
    fields: list[FieldDescription]
    filter_fields: list[str]
    sort_fields: list[str]
    search_fields: list[str]
    conditional: bool = True
    versioned: bool
    default_limit: int | None
    max_limit: int | None
    max_batch: int | None
    capabilities: dict[str, bool]


class ResourceDescriptions(ApiResponse):
    resources: list[ResourceDescription]


def property_schema(document: dict[str, Any], name: str) -> dict[str, Any]:
    result: dict[str, Any] = deepcopy(document["properties"][name])
    definitions: dict[str, Any] = {}

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            ref = value.get("$ref", "")
            if ref.startswith("#/$defs/"):
                key = ref.removeprefix("#/$defs/")
                if key not in definitions:
                    definitions[key] = deepcopy(document["$defs"][key])
                    visit(definitions[key])
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(result)
    if definitions:
        result["$defs"] = definitions
    return result


def field_presentation(spec: Any, definition: dict[str, Any], reference: str | None) -> Any:
    column = spec.column
    rule = column.info.get("obox_files")
    if rule is not None:
        return "attachment", {
            "contentTypes": sorted(set(rule.types.values())) if rule.types is not None else None,
            "maxSize": rule.max_size,
            "minCount": rule.min_count,
            "maxCount": rule.max_count,
        }
    options = {}
    pending = [definition]
    visited = set()
    while pending:
        item = pending.pop()
        if "$ref" in item:
            if item["$ref"] in visited:
                continue
            visited.add(item["$ref"])
            item = definition.get("$defs", {}).get(item["$ref"].rsplit("/", 1)[-1], {})
        options.update(
            {
                key: item[key]
                for key in (
                    "enum",
                    "format",
                    "minLength",
                    "maxLength",
                    "minimum",
                    "maximum",
                    "exclusiveMinimum",
                    "exclusiveMaximum",
                    "multipleOf",
                    "pattern",
                )
                if key in item
            }
        )
        pending.extend(item.get("anyOf", []))
    kind = next(
        (
            kind
            for cls, kind in (
                (sa.Enum, "select"),
                (sa.Boolean, "boolean"),
                (sa.Integer, "integer"),
                (sa.Float, "number"),
                (sa.Numeric, "decimal"),
                (sa.DateTime, "datetime"),
                (sa.Date, "date"),
                (sa.Time, "time"),
                (sa.Uuid, "uuid"),
                (sa.JSON, "json"),
            )
            if isinstance(column.type, cls)
        ),
        "text",
    )
    if isinstance(column.type, sa.Numeric) and not isinstance(column.type, sa.Float):
        options.update(precision=column.type.precision, scale=column.type.scale)
    if isinstance(column.type, sa.Enum) and "enum" not in options:
        options["enum"] = list(column.type.enums)
    if column.computed is not None:
        kind = "computed"
    elif reference is not None:
        kind = "relationship"
    return kind, options


async def describe(app: Any, builder: Any, ctx: Context, record_schema: Any) -> ResourceDescription:
    service = app.service(builder.model)
    operations = {name: await service.permitted(name, ctx) for name in builder.api.operations}
    scopes = {
        action: builder.permission_name(action)
        for action in (*builder.api.operations, *builder.permissions)
    }
    for name in builder.actions:
        permission = builder.action_methods[name].obox_action[1]
        operations[to_camel(name)] = await service.permitted(permission, ctx)
        scopes[to_camel(name)] = builder.permission_name(permission)
    if not any(operations.values()):
        raise NotFound("Resource metadata is unavailable.")
    readable = set(builder.readable(ctx, fields=builder.api_read))
    writable = {
        action: set(builder.writable(ctx, action, surface="api"))
        if operations.get(action)
        else set()
        for action in ("create", "update")
    }
    visible = readable | writable["create"] | writable["update"]
    read_schema = record_schema.model_json_schema(mode="serialization")
    create_schema = builder.create_schema.model_json_schema()
    update_schema = builder.update_schema.model_json_schema()
    input_schemas = {"create": create_schema, "update": update_schema}
    fields = []
    for name, spec in builder.specs.items():
        if name not in visible:
            continue
        alias = builder.api_names[name]
        document = next(
            (
                document
                for document in (read_schema, create_schema, update_schema)
                if alias in document["properties"]
            ),
            None,
        )
        if document is None:
            continue
        reference = None
        for fk in spec.column.foreign_keys:
            target = app._for_table(fk.column.table)
            if (
                target is not None
                and target.api.enabled
                and "retrieve" in target.api.operations
                and await app.service(target.model).permitted("retrieve", ctx)
            ):
                reference = target.api_key
        editable = {
            action: name in values and alias in input_schemas[action]["properties"]
            for action, values in writable.items()
        }
        definition = property_schema(document, alias)
        kind, options = field_presentation(spec, definition, reference)
        fields.append(
            FieldDescription(
                name=alias,
                schema=definition,
                nullable=spec.column.nullable,
                readable=name in readable,
                read_only=not any(editable.values()),
                required=editable["create"] and spec.required,
                writable=editable,
                reference=reference,
                kind=kind,
                options=options,
                computed=spec.column.computed is not None,
            )
        )
    return ResourceDescription(
        slug=builder.api_key,
        path=(ctx.request.scope.get("root_path", "") if ctx.request is not None else "")
        + builder.api_path,
        operations=operations,
        scopes={to_camel(name): value for name, value in scopes.items()},
        fields=fields,
        filter_fields=[
            builder.api_names[name] for name in builder.filter_fields if name in readable
        ]
        if operations.get("list")
        else [],
        sort_fields=[builder.api_names[name] for name in builder.sort_fields if name in readable]
        if operations.get("list")
        else [],
        search_fields=[
            builder.api_names[name] for name in builder.search_fields if name in readable
        ]
        if operations.get("list")
        else [],
        versioned=builder.version_column is not None,
        default_limit=builder.api.default_limit,
        max_limit=builder.api.max_limit,
        max_batch=builder.api.max_batch,
        capabilities={
            "attachments": app.get_plugin("files") is not None
            and any(field.kind == "attachment" and field.readable for field in fields),
            "comments": app.get_plugin("activity") is not None
            and bool(operations.get("retrieve"))
            and await service.permitted("comment", ctx),
            "webhooks": app.get_plugin("webhooks") is not None
            and bool(operations.get("retrieve"))
            and bool(getattr(ctx.principal, "is_authenticated", False))
            and bool(getattr(ctx.principal, "is_active", False))
            and getattr(ctx.principal, "api_scopes", None) is None,
        },
    )
