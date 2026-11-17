"""Shared templates and navigation for optional applications."""

from collections.abc import Sequence
from typing import Any, cast

import sqlalchemy as sa
from fastapi import Request
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, Environment, FileSystemLoader, PackageLoader, select_autoescape

from obox._catalog import CatalogConfig
from obox._config import RuntimeConfig
from obox._context import Context
from obox._exceptions import BuilderValidationError
from obox._filters import parse_filter


async def build_navigation(app: Any, request: Request, ctx: Context) -> Any:
    from obox._navigation import navigation

    return await navigation(app, request, ctx)


def create_templates(
    settings: RuntimeConfig, apps: Sequence[CatalogConfig] = ()
) -> Jinja2Templates:
    if settings.templates is not None:
        return settings.templates
    env = Environment(
        loader=ChoiceLoader(
            [
                FileSystemLoader(settings.template_dirs),
                *(
                    PackageLoader(config.name, "templates")
                    for config in apps
                    if config.has_directory("templates")
                ),
                PackageLoader("obox", "templates"),
                PackageLoader("obox.contrib.auth", "templates"),
                PackageLoader("obox.contrib.files", "templates"),
                PackageLoader("obox.contrib.analytics", "templates"),
            ]
        ),
        autoescape=select_autoescape(("html",)),
        extensions=["jinja2.ext.i18n"],
    )
    cast(Any, env).install_null_translations()
    return Jinja2Templates(env=env)


def column_kind(column_type: Any) -> str:
    if isinstance(column_type, sa.Enum):
        return "choice"
    if isinstance(column_type, sa.Boolean):
        return "boolean"
    if isinstance(column_type, sa.String):
        return "text"
    if isinstance(column_type, (sa.Integer, sa.Numeric, sa.Float)):
        return "number"
    if isinstance(column_type, sa.DateTime):
        return "datetime"
    if isinstance(column_type, sa.Date):
        return "date"
    if isinstance(column_type, sa.Time):
        return "time"
    return "text"


def filter_columns(builder: Any, ctx: Context, requested: Sequence[str]) -> list[dict[str, Any]]:
    readable = set(builder.readable(ctx))
    columns = []
    for name in requested:
        if name not in builder.filter_fields or name not in readable:
            continue
        column_type = builder.specs[name].column.type
        kind = column_kind(column_type)
        choices = list(column_type.enums) if isinstance(column_type, sa.Enum) else []
        columns.append(
            {
                "name": name,
                "label": name.replace("_", " ").capitalize(),
                "kind": kind,
                "choices": choices,
                "operators": (
                    ["is_empty", "is_not_empty"]
                    if "obox_files" in builder.specs[name].column.info
                    else [
                        "contains",
                        "does_not_contain",
                        "is",
                        "is_not",
                        "is_empty",
                        "is_not_empty",
                    ]
                    if kind == "text"
                    else ["is", "is_not", "is_empty", "is_not_empty"]
                ),
            }
        )
    return columns


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
