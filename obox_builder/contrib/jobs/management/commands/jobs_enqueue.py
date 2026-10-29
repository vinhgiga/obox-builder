from __future__ import annotations

import json
from datetime import datetime

import typer

from obox_builder.core.management.base import CommandError, command


@command(help="Enqueue a registered task with JSON arguments.", requires_checks=True)
async def handle(
    name: str,
    args: str = typer.Option("[]", "--args"),
    kwargs: str = typer.Option("{}", "--kwargs"),
    eta: datetime | None = typer.Option(
        None, "--eta", help="Earliest execution, including timezone."
    ),
    queue: str | None = typer.Option(None, "--queue"),
    dedup_key: str | None = typer.Option(None, "--dedup-key"),
) -> None:
    from obox_builder.contrib.jobs import enqueue
    from obox_builder.core.management.utils import echo
    from obox_builder.db.session import session_scope

    try:
        positional, keywords = json.loads(args), json.loads(kwargs)
    except ValueError:
        raise CommandError("args and kwargs must be valid JSON.") from None
    try:
        async with session_scope() as db:
            row = await enqueue(
                db,
                name,
                args=positional,
                kwargs=keywords,
                eta=eta,
                queue=queue,
                dedup_key=dedup_key,
            )
    except (TypeError, ValueError) as exc:
        raise CommandError(str(exc)) from exc
    echo(row.id)
