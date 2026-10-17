from __future__ import annotations

import logging
import os
from collections.abc import Callable
from importlib import import_module
from pathlib import Path
from typing import Any, Literal

from alembic.config import Config

from obox_builder.conf import SETTINGS_MODULE_ENV
from obox_builder.core.exceptions import ImproperlyConfigured

MIGRATIONS_DIR_NAME = "migrations"


def configure_alembic_logging(verbosity: int) -> None:
    """Alembic logs at INFO by default; only show that at verbosity >= 2."""
    logging.getLogger("alembic").setLevel(logging.INFO if verbosity >= 2 else logging.WARNING)


def settings_module_path() -> Path:
    module_name = os.environ.get(SETTINGS_MODULE_ENV)
    if not module_name:
        raise ImproperlyConfigured(
            f"{SETTINGS_MODULE_ENV} is not set; migrations need a settings module."
        )
    module = import_module(module_name)
    module_file = getattr(module, "__file__", None)
    if not module_file:
        raise ImproperlyConfigured(f"Settings module {module_name!r} has no filesystem path.")
    return Path(module_file).resolve()


def project_root() -> Path:
    """Project root = parent of the directory containing the settings module."""
    return settings_module_path().parent.parent


def migrations_dir() -> Path:
    return project_root() / MIGRATIONS_DIR_NAME


def build_config() -> Config:
    """Build an Alembic ``Config`` in code (no ``alembic.ini`` needed)."""
    config = Config()
    root = project_root()
    config.set_main_option("script_location", str(root / MIGRATIONS_DIR_NAME))
    config.attributes["project_root"] = str(root)
    return config


def installed_table_names() -> set[str]:
    """Table names owned by installed apps (models plus bare association tables)."""
    from obox_builder.apps import apps

    names = {model.__table__.name for model in apps.get_models() if hasattr(model, "__table__")}
    names.update(table.name for table in apps.get_tables())
    return names


def make_include_object() -> Callable[[Any, str | None, str, bool, Any], bool]:
    """Alembic ``include_object``: only tables of installed apps are managed."""
    installed_tables = installed_table_names()

    def include_object(
        object_: Any, name: str | None, type_: str, reflected: bool, compare_to: Any
    ) -> bool:
        if type_ == "table":
            if reflected:
                return False
            return name in installed_tables
        table = getattr(object_, "table", None)
        if table is not None:
            return table.name in installed_tables
        return True

    return include_object


def obox_render_item(type_: str, object_: Any, autogen_context: Any) -> Literal[False]:
    """Alembic ``render_item`` hook for imports the default renderer misses.

    ``postgresql.JSONB`` reprs as ``JSONB(astext_type=Text())``, so generated
    migrations need ``Text`` imported or they fail at runtime.
    """
    variant_mapping = getattr(object_, "_variant_mapping", None)
    if (
        type_ == "type"
        and isinstance(variant_mapping, dict)
        and any("JSONB" in type(variant).__name__ for variant in variant_mapping.values())
    ):
        autogen_context.imports.add("from sqlalchemy import Text")
    return False
