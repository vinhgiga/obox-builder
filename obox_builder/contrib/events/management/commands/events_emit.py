from __future__ import annotations

import json

import typer

from obox_builder.core.management.base import CommandError, command


@command(help="Publish a JSON domain event in its own transaction.", requires_checks=True)
async def handle(
    type: str = typer.Argument(...),
    payload: str = typer.Option("{}", "--payload"),
    subject: str | None = typer.Option(None, "--subject"),
    dedup_key: str | None = typer.Option(None, "--dedup-key"),
) -> None:
    from obox_builder.contrib.events import publish
    from obox_builder.contrib.history.context import use
    from obox_builder.core.management.utils import echo
    from obox_builder.db.session import session_scope

    try:
        data = json.loads(payload)
        if not isinstance(data, dict):
            raise ValueError("Event payload must be a JSON object.")
        async with use(source="cli", actor_label="cli"), session_scope() as db:
            event_id = await publish(db, type, data, subject=subject, dedup_key=dedup_key)
            if event_id is None:
                raise CommandError("Events are disabled.")
    except (TypeError, ValueError) as exc:
        raise CommandError(str(exc)) from exc
    echo(f"Committed event draft {event_id} (a duplicate dedup key keeps the original event).")
