from __future__ import annotations

import typer

from obox_builder.core.management import utils
from obox_builder.core.management.base import CommandError, command

DRIFT_SAMPLE = 50


@command(help="Compare tracked rows with their recorded history; --fix repairs drift.")
def handle(
    model: str = typer.Option("", "--model", help="Only this model label (app.model)."),
    batch_size: int = typer.Option(10_000, "--batch-size", help="Rows per batch (max 10000)."),
    fix: bool = typer.Option(False, "--fix", help="Append repair entries (source=db_trigger)."),
) -> None:
    import obox_builder
    from obox_builder.contrib.history.reconcile import MAX_BATCH_SIZE, reconcile
    from obox_builder.contrib.history.service import MODEL_LABEL_RE
    from obox_builder.db.engine import run_async
    from obox_builder.db.session import session_scope

    if model and not MODEL_LABEL_RE.match(model):
        raise CommandError(f"Invalid model label {model!r}; expected 'app.model'.")
    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise CommandError(f"--batch-size must be between 1 and {MAX_BATCH_SIZE}.")

    async def run() -> None:
        async with session_scope() as db:
            result = await reconcile(db, model=model or None, batch_size=batch_size, fix=fix)
        for drift in result.drifts[:DRIFT_SAMPLE]:
            utils.echo(drift.describe(), level=1)
        if len(result.drifts) > DRIFT_SAMPLE:
            utils.echo(f"... and {len(result.drifts) - DRIFT_SAMPLE} more drift(s).", level=1)
        if result.skipped_labels:
            skipped = ", ".join(result.skipped_labels)
            utils.echo(f"Skipped (model not registered): {skipped}.", level=1)
        verb = "Repaired" if fix else "Found"
        utils.echo(
            f"Checked {result.checked} row(s): "
            f"{len(result.drifts)} drift(s); {verb} {result.fixed}."
        )

    obox_builder.setup()
    run_async(run())
