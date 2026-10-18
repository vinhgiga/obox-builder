from __future__ import annotations

import typer

from obox_builder.core.management import utils
from obox_builder.core.management.base import CommandError, command


@command(help="Seal old history entries into hash-chained tamper-evidence blocks.")
def handle(
    lag_seconds: int = typer.Option(
        600, "--lag", help="Only seal entries older than this many seconds."
    ),
    batch_size: int = typer.Option(10_000, "--batch-size", help="Entries per block (max 10000)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report without sealing."),
) -> None:
    import obox_builder
    from obox_builder.contrib.history.seal import MAX_BATCH_SIZE, seal
    from obox_builder.db.engine import run_async
    from obox_builder.db.session import session_scope

    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise CommandError(f"--batch-size must be between 1 and {MAX_BATCH_SIZE}.")
    if lag_seconds < 0:
        raise CommandError("--lag must be >= 0.")

    async def run() -> None:
        async with session_scope() as db:
            result = await seal(db, lag_seconds=lag_seconds, batch_size=batch_size, dry_run=dry_run)
        verb = "Would seal" if dry_run else "Sealed"
        utils.echo(f"{verb} {result.sealed} entry(ies) in {result.blocks} block(s).")
        if dry_run and result.dry_run_ids is not None:
            first_id, last_id = result.dry_run_ids
            utils.echo(f"Range: [{first_id}..{last_id}].", level=1)

    obox_builder.setup()
    run_async(run())
