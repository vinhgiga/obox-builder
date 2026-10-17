from __future__ import annotations

from pathlib import Path
from typing import cast

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from alembic.script import Script, ScriptDirectory
from sqlalchemy import create_engine

from obox_builder.conf import settings
from obox_builder.db.base import ModelBase
from obox_builder.db.engine import to_sync_url
from obox_builder.db.migrations.config import build_config, make_include_object


def _prepare() -> None:
    import obox_builder

    obox_builder.setup()


def _has_changes() -> bool:
    engine = create_engine(to_sync_url(settings.DATABASE_URL))
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection,
                opts={"compare_type": True, "include_object": make_include_object()},
            )
            diff = compare_metadata(context, ModelBase.metadata)
    finally:
        engine.dispose()
    return bool(diff)


def make_migration(message: str | None = None, *, empty: bool = False) -> Path | None:
    """Autogenerate a revision; returns the file path, or None when unchanged."""
    _prepare()
    config = build_config()
    if not empty and not _has_changes():
        return None
    script = cast(
        "Script | None", command.revision(config, message=message or "", autogenerate=not empty)
    )
    if script is None:  # pragma: no cover - alembic always returns a Script here
        return None
    return Path(script.path)


def upgrade(revision: str = "head", *, sql: bool = False) -> None:
    _prepare()
    config = build_config()
    config.attributes["connection_url"] = to_sync_url(settings.DATABASE_URL)
    command.upgrade(config, revision, sql=sql)


def current_revision() -> str | None:
    _prepare()
    engine = create_engine(to_sync_url(settings.DATABASE_URL))
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection)
            return context.get_current_revision()
    finally:
        engine.dispose()


def migration_history() -> list[tuple[str, bool]]:
    """Return ``(revision, is_applied)`` pairs, oldest first."""
    _prepare()
    config = build_config()
    scripts = ScriptDirectory.from_config(config)
    current = current_revision()
    applied: set[str] = set()
    if current is not None:
        applied = {rev.revision for rev in scripts.iterate_revisions(current, "base")}
    revisions = list(scripts.walk_revisions())
    return [(script.revision, script.revision in applied) for script in reversed(revisions)]
