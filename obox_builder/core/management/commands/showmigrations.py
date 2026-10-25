from __future__ import annotations

import typer

from obox_builder.core.management import utils
from obox_builder.core.management.base import command


@command(help="Show migration status.", requires_checks=True)
def handle(
    database: str = typer.Option("default", "--database", help="Database alias to inspect."),
) -> None:
    from obox_builder.db.migrations import configure_alembic_logging, migration_history

    configure_alembic_logging(utils.get_verbosity())
    history = migration_history(database)
    if not history:
        utils.echo("No migrations found.")
        return
    for revision, applied in history:
        marker = "[X]" if applied else "[ ]"
        utils.echo(f" {marker} {revision}")
