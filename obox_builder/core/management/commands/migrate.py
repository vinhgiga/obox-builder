from __future__ import annotations

import asyncio

import typer

from obox_builder.core.management import utils
from obox_builder.core.management.base import command
from obox_builder.core.signals import post_migrate


@command(help="Apply database migrations.", requires_checks=True)
async def handle(
    target: str = typer.Argument("head", help="Revision to upgrade to (default: head)."),
    sql: bool = typer.Option(False, "--sql", help="Print the SQL instead of applying it."),
    database: str = typer.Option("default", "--database", help="Database alias to migrate."),
) -> None:
    from obox_builder.db.migrations import configure_alembic_logging, upgrade

    configure_alembic_logging(utils.get_verbosity())
    await asyncio.to_thread(upgrade, target, sql=sql, database=database)
    if not sql:
        await post_migrate.send_async(sender="migrate", database=database)
    utils.echo(f"Applied migrations up to {target!r} on {database!r}.")
