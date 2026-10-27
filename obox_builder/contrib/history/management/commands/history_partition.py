from __future__ import annotations

import typer

from obox_builder.core.management import utils
from obox_builder.core.management.base import CommandError, command


@command(
    help=(
        "Manage monthly partitions of obox_row_change (PostgreSQL). "
        "Exactly one of --ensure / --drop-before / --sql is required."
    )
)
def handle(
    ensure: bool = typer.Option(
        False, "--ensure", help="Create the coming month partitions + DEFAULT."
    ),
    months: int = typer.Option(2, "--months", help="How many months to ensure (with --ensure)."),
    drop_before: str = typer.Option(
        "", "--drop-before", help="Retention: drop partitions ending before YYYY-MM."
    ),
    sql: bool = typer.Option(
        False, "--sql", help="Print the conversion DDL (review before applying)."
    ),
    cutoff: str = typer.Option(
        "", "--cutoff", help="Month start (YYYY-MM) where old entries end (with --sql)."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report without changing anything."),
) -> None:
    import obox_builder
    from obox_builder.contrib.history.partition import (
        PartitionError,
        conversion_sql,
        next_month_cutoff,
        parse_month,
    )
    from obox_builder.contrib.history.partition import (
        drop_before as drop_partitions,
    )
    from obox_builder.contrib.history.partition import (
        ensure as ensure_partitions,
    )
    from obox_builder.db.engine import run_async
    from obox_builder.db.session import session_scope

    modes = sum((ensure, bool(drop_before), sql))
    if modes != 1:
        raise CommandError("Pass exactly one of --ensure, --drop-before or --sql.")
    if not 1 <= months <= 120:
        raise CommandError("--months must be between 1 and 120.")
    if dry_run and sql:
        raise CommandError("--dry-run has no effect with --sql.")

    if sql:
        try:
            start = parse_month(cutoff) if cutoff else next_month_cutoff()
        except PartitionError as error:
            raise CommandError(str(error)) from error
        for statement in conversion_sql(start):
            utils.echo(statement)
        return

    async def run() -> None:
        async with session_scope() as db:
            if ensure:
                created = await ensure_partitions(db, months=months, dry_run=dry_run)
                verb = "Would create" if dry_run else "Created"
                utils.echo(f"{verb} {len(created.created)} partition(s).")
                for name in created.created:
                    utils.echo(f"  {name}", level=1)
                if created.kept:
                    utils.echo(f"Kept {len(created.kept)} existing partition(s).", level=1)
                return
            dropped = await drop_partitions(db, month=drop_before, dry_run=dry_run)
            verb = "Would drop" if dry_run else "Dropped"
            utils.echo(f"{verb} {len(dropped.dropped)} partition(s).")
            for name in dropped.dropped:
                utils.echo(f"  {name}", level=1)
            utils.echo(f"Rows removed: {dropped.rows_dropped}.")

    try:
        obox_builder.setup()
        run_async(run())
    except PartitionError as error:
        raise CommandError(str(error)) from error
