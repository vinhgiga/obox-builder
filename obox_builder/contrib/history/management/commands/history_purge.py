from __future__ import annotations

import re

import typer

from obox_builder.core.management import utils
from obox_builder.core.management.base import CommandError, command

MODEL_LABEL_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")


@command(help="Delete history entries past their retention window.", requires_checks=True)
def handle(
    days: int | None = typer.Option(
        None, "--days", help="Retention window, overriding per-model/global settings."
    ),
    model: str = typer.Option("", "--model", help="Only purge this model label (app.model)."),
    batch_size: int = typer.Option(10_000, "--batch-size", help="Rows per batch (max 10000)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report without deleting."),
) -> None:
    import obox_builder
    from obox_builder.contrib.history.purge import MAX_BATCH_SIZE, purge
    from obox_builder.db.engine import run_async
    from obox_builder.db.session import session_scope

    if model and not MODEL_LABEL_RE.match(model):
        raise CommandError(f"Invalid model label {model!r}; expected 'app_label.model_name'.")
    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise CommandError(f"--batch-size must be between 1 and {MAX_BATCH_SIZE}.")

    async def run() -> None:
        async with session_scope() as db:
            result = await purge(db, days=days, model=model, batch_size=batch_size, dry_run=dry_run)
        if dry_run:
            utils.echo(f"Would delete {result.deleted} row(s).")
        else:
            utils.echo(f"Deleted {result.deleted} row(s) in {result.batches} batch(es).")
        for label, count in sorted(result.per_label.items()):
            if count:
                utils.echo(f"  {label}: {count}", level=2)
        if result.skipped:
            skipped = ", ".join(result.skipped)
            utils.echo(f"No retention configured (skipped): {skipped}", level=2)

    obox_builder.setup()
    run_async(run())
