from __future__ import annotations

import re

import typer

from obox_builder.core.management import utils
from obox_builder.core.management.base import CommandError, command

MODEL_LABEL_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")


@command(help="Erase actor or row values from history (entries are never deleted).")
def handle(
    actor_id: int | None = typer.Option(
        None, "--actor-id", help="Blank the actor trail of this user id."
    ),
    model: str = typer.Option("", "--model", help="Row scope: model label (app.model)."),
    pk: str = typer.Option("", "--pk", help="Row scope: object primary key."),
    batch_size: int = typer.Option(10_000, "--batch-size", help="Rows per batch (max 10000)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report without updating."),
) -> None:
    import obox_builder
    from obox_builder.contrib.history.anonymize import (
        MAX_BATCH_SIZE,
        anonymize_actor,
        anonymize_object,
    )
    from obox_builder.db.engine import run_async
    from obox_builder.db.session import session_scope

    if actor_id is None and not (model or pk):
        raise CommandError("Pass --actor-id or both --model and --pk.")
    if actor_id is not None and (model or pk):
        raise CommandError("--actor-id cannot be combined with --model/--pk.")
    if (model or pk) and not (model and pk):
        raise CommandError("--model and --pk must be passed together.")
    if model and not MODEL_LABEL_RE.match(model):
        raise CommandError(f"Invalid model label {model!r}; expected 'app_label.model_name'.")
    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise CommandError(f"--batch-size must be between 1 and {MAX_BATCH_SIZE}.")

    async def run() -> None:
        async with session_scope() as db:
            if actor_id is not None:
                result = await anonymize_actor(db, actor_id, batch_size=batch_size, dry_run=dry_run)
                scope = f"actor_id={actor_id}"
            else:
                result = await anonymize_object(
                    db,
                    model_label=model,
                    object_pk=pk,
                    batch_size=batch_size,
                    dry_run=dry_run,
                )
                scope = f"{model}#{pk}"
        verb = "Would update" if dry_run else "Updated"
        utils.echo(f"{verb} {result.updated} row(s) ({scope}).")

    obox_builder.setup()
    run_async(run())
