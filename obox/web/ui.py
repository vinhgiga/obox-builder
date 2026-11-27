"""Shared templates and navigation for optional applications."""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from fastapi import Request
from fastapi.templating import Jinja2Templates

from obox.apps import AppConfig
from obox.db.query import field_kind, filter_operators
from obox.runtime.config import RuntimeConfig
from obox.runtime.context import Context


async def build_navigation(app: Any, request: Request, ctx: Context) -> Any:
    from obox.web.navigation import navigation

    return await navigation(app, request, ctx)


def create_templates(settings: RuntimeConfig, apps: Sequence[AppConfig] = ()) -> Jinja2Templates:
    from importlib import import_module
    from pathlib import Path

    from obox.conf import settings as project_settings

    definition: dict[str, Any] = (
        dict(project_settings.TEMPLATES[0])
        if project_settings.configured and project_settings.TEMPLATES
        else {"BACKEND": "obox.template.backends.jinja2.Jinja2", "APP_DIRS": True}
    )
    directories = list(settings.template_dirs)
    if definition.get("APP_DIRS", False):
        directories.extend(str(Path(app.path) / "templates") for app in apps)
    definition["DIRS"] = directories
    module, _, name = definition["BACKEND"].rpartition(".")
    return Jinja2Templates(env=getattr(import_module(module), name)(definition).env)


def filter_columns(builder: Any, ctx: Context, requested: Sequence[str]) -> list[dict[str, Any]]:
    readable = set(builder.readable(ctx))
    columns = []
    for name in requested:
        if name not in builder.filter_fields or name not in readable:
            continue
        column_type = builder.specs[name].column.type
        kind = field_kind(builder.specs[name].column)
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
                    else filter_operators(kind)
                ),
            }
        )
    return columns
