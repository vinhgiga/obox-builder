from __future__ import annotations

from datetime import datetime

import typer

from obox_builder.core.management.base import CommandError, command


@command(help="Create missing deliveries for a bounded event time window.", requires_checks=True)
async def handle(
    subscription: str,
    since: str = typer.Option(..., "--since", help="ISO timestamp with timezone."),
    until: str = typer.Option(..., "--until", help="Exclusive ISO timestamp with timezone."),
    limit: int = typer.Option(1000, "--limit", min=1, max=10_000),
) -> None:
    from obox_builder.contrib.events.services import replay
    from obox_builder.core.management.utils import echo
    from obox_builder.db.session import session_scope

    try:
        async with session_scope() as db:
            count = await replay(
                db,
                subscription,
                since=datetime.fromisoformat(since),
                until=datetime.fromisoformat(until),
                limit=limit,
            )
    except ValueError as exc:
        raise CommandError(str(exc)) from exc
    echo(f"Created {count} missing deliveries (limit {limit}).")
