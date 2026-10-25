from __future__ import annotations

from pathlib import Path
from typing import cast

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from alembic.script import Script, ScriptDirectory
from sqlalchemy import create_engine

from obox_builder.conf import settings
from obox_builder.core.exceptions import ImproperlyConfigured
from obox_builder.db.base import ModelBase
from obox_builder.db.engine import to_sync_url
from obox_builder.db.migrations.config import build_config, make_include_object, version_dir


def _prepare() -> None:
    import obox_builder

    obox_builder.setup()


def _database_url(database: str) -> str:
    try:
        return settings.database_urls[database]
    except KeyError:
        configured = ", ".join(sorted(settings.database_urls))
        raise ImproperlyConfigured(
            f"Unknown database alias {database!r}. Configured aliases: {configured}."
        ) from None


def _has_changes(database: str) -> bool:
    engine = create_engine(to_sync_url(_database_url(database)))
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection,
                opts={"compare_type": True, "include_object": make_include_object(database)},
            )
            diff = compare_metadata(context, ModelBase.metadata)
    finally:
        engine.dispose()
    return bool(diff)


def make_migration(
    message: str | None = None, *, empty: bool = False, database: str = "default"
) -> Path | None:
    """Autogenerate a revision; returns the file path, or None when unchanged."""
    _prepare()
    _database_url(database)
    databases = tuple(settings.database_urls)
    if not empty and not any(_has_changes(alias) for alias in databases):
        return None
    config = build_config(database, create_version_dir=True)
    config.attributes["autogenerate_multi_db"] = True
    script = cast(
        "Script | None", command.revision(config, message=message or "", autogenerate=not empty)
    )
    if script is None:  # pragma: no cover - alembic always returns a Script here
        return None
    return Path(script.path)


def upgrade(revision: str = "head", *, sql: bool = False, database: str = "default") -> None:
    _prepare()
    _database_url(database)
    if database != "default" and not version_dir(database).exists():
        return
    config = build_config(database)
    config.attributes["connection_url"] = to_sync_url(_database_url(database))
    command.upgrade(config, revision, sql=sql)


def current_revision(database: str = "default") -> str | None:
    _prepare()
    engine = create_engine(to_sync_url(_database_url(database)))
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection)
            return context.get_current_revision()
    finally:
        engine.dispose()


def migration_history(database: str = "default") -> list[tuple[str, bool]]:
    """Return ``(revision, is_applied)`` pairs, oldest first."""
    _prepare()
    if database != "default" and not version_dir(database).exists():
        return []
    config = build_config(database)
    scripts = ScriptDirectory.from_config(config)
    current = current_revision(database)
    applied: set[str] = set()
    if current is not None:
        applied = {rev.revision for rev in scripts.iterate_revisions(current, "base")}
    revisions = list(scripts.walk_revisions())
    return [(script.revision, script.revision in applied) for script in reversed(revisions)]
