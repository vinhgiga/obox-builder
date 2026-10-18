from __future__ import annotations

import typer

from obox_builder.core.management import utils
from obox_builder.core.management.base import CommandError, command


@command(help="Rewrite the model label of history entries after a model rename.")
def handle(
    from_label: str = typer.Option(..., "--from", help="Current model label (app.model)."),
    to_label: str = typer.Option(..., "--to", help="New model label (app.model)."),
    batch_size: int = typer.Option(10_000, "--batch-size", help="Rows per batch (max 10000)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report without updating."),
) -> None:
    import obox_builder
    from obox_builder.contrib.history.relabel import MAX_BATCH_SIZE, relabel
    from obox_builder.contrib.history.service import MODEL_LABEL_RE
    from obox_builder.db.engine import run_async
    from obox_builder.db.session import session_scope

    for name, label in (("--from", from_label), ("--to", to_label)):
        if not MODEL_LABEL_RE.match(label):
            raise CommandError(f"Invalid model label {label!r} for {name}; expected 'app.model'.")
    if from_label == to_label:
        raise CommandError("--from and --to must differ.")
    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise CommandError(f"--batch-size must be between 1 and {MAX_BATCH_SIZE}.")

    async def run() -> None:
        async with session_scope() as db:
            result = await relabel(
                db,
                from_label=from_label,
                to_label=to_label,
                batch_size=batch_size,
                dry_run=dry_run,
            )
        verb = "Would update" if dry_run else "Updated"
        utils.echo(f"{verb} {result.updated} row(s): {from_label} -> {to_label}.")

    obox_builder.setup()
    run_async(run())
