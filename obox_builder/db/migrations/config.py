from __future__ import annotations

import hashlib
import logging
import os
import re
from collections.abc import Callable
from importlib import import_module
from pathlib import Path
from typing import Any, Literal

from alembic.config import Config

from obox_builder.conf import SETTINGS_MODULE_ENV, settings
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
    configured = getattr(settings, "MIGRATIONS_DIR", None)
    if configured is not None:
        return Path(configured)
    return project_root() / MIGRATIONS_DIR_NAME


def version_dir(database: str = "default") -> Path:
    """Return the shared Alembic version folder used by all database aliases."""
    del database
    return migrations_dir() / "versions"


def database_key(database: str) -> str:
    """Return a stable, Python-safe engine name for Alembic's multidb template."""
    slug = re.sub(r"[^A-Za-z0-9_]+", "_", database).strip("_")[:32] or "alias"
    if slug[0].isdigit():
        slug = f"_{slug}"
    suffix = hashlib.sha256(database.encode()).hexdigest()[:8]
    return f"db_{slug}_{suffix}"


def build_config(database: str = "default", *, create_version_dir: bool = False) -> Config:
    """Build an Alembic ``Config`` with one shared revision graph."""
    config = Config()
    root = project_root()
    config.set_main_option("script_location", str(migrations_dir()))
    file_template = getattr(
        settings,
        "ALEMBIC_FILE_TEMPLATE",
        "%(year)d%(month).2d%(day).2d%(hour).2d%(minute).2d%(second).2d_%(slug)s",
    )
    if file_template:
        config.set_main_option("file_template", file_template.replace("%", "%%"))
    config.set_main_option("path_separator", "os")
    versions = version_dir(database)
    if create_version_dir:
        versions.mkdir(parents=True, exist_ok=True)
    aliases = tuple(settings.database_urls)
    keys = {alias: database_key(alias) for alias in aliases}
    config.set_main_option("databases", ",".join(keys.values()))
    config.attributes["project_root"] = str(root)
    config.attributes["database_alias"] = database
    config.attributes["database_aliases"] = aliases
    config.attributes["database_keys"] = keys
    return config


def installed_table_names() -> set[str]:
    """Table names owned by installed apps (models plus bare association tables)."""
    from obox_builder.apps import apps

    names = {model.__table__.name for model in apps.get_models() if hasattr(model, "__table__")}
    names.update(table.name for table in apps.get_tables())
    return names


def make_include_object(
    database: str = "default",
) -> Callable[[Any, str | None, str, bool, Any], bool]:
    """Alembic ``include_object``: only tables of installed apps are managed."""
    from obox_builder.apps import apps
    from obox_builder.db.router import router

    table_owners: dict[str, tuple[str, type[Any] | None]] = {}
    for model in apps.get_models():
        if hasattr(model, "__table__"):
            table_owners[model.__table__.name] = (model._model_label, model)
    for app_label, tables in apps.all_tables.items():
        for table in tables.values():
            table_owners[table.name] = (app_label, None)

    def allowed(table_name: str) -> bool:
        owner = table_owners.get(table_name)
        if owner is None:
            return False
        app_label, model = owner
        if model is not None:
            return router.allow_migrate_model(database, model)
        return router.allow_migrate(database, app_label)

    def include_object(
        object_: Any, name: str | None, type_: str, reflected: bool, compare_to: Any
    ) -> bool:
        if type_ == "table":
            if reflected:
                return False
            return name is not None and allowed(name)
        table = getattr(object_, "table", None)
        if table is not None:
            return allowed(table.name)
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
