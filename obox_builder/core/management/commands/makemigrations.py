from __future__ import annotations

import typer

from obox_builder.core.management import utils
from obox_builder.core.management.base import command


@command(help="Autogenerate a migration from model changes.", requires_checks=True)
def handle(
    message: str = typer.Option(
        "", "-m", "--message", help="Migration message (for example 'add post table')."
    ),
    empty: bool = typer.Option(False, "--empty", help="Create an empty migration."),
) -> None:
    from obox_builder.db.migrations import configure_alembic_logging, make_migration

    configure_alembic_logging(utils.get_verbosity())
    path = make_migration(message or None, empty=empty)
    if path is None:
        utils.echo("No changes detected.")
    else:
        utils.echo(f"Created migration {path.name}.")
