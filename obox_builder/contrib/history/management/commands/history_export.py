from __future__ import annotations

import contextlib
from datetime import timedelta

import typer

from obox_builder.core.management import utils
from obox_builder.core.management.base import CommandError, command


@command(help="Export history entries as NDJSON or CSV.")
def handle(
    format: str = typer.Option("ndjson", "--format", help="ndjson or csv."),
    model: str = typer.Option("", "--model", help="Only entries of this model label (app.model)."),
    since: str = typer.Option("", "--since", help="Start date (YYYY-MM-DD)."),
    until: str = typer.Option("", "--until", help="End date (YYYY-MM-DD, inclusive)."),
    actor: str = typer.Option("", "--actor", help="Actor id or actor label substring."),
    output: str = typer.Option("", "--output", help="Write to this path (default: stdout)."),
) -> None:
    import obox_builder
    from obox_builder.contrib.history import export
    from obox_builder.contrib.history.service import FeedFilters
    from obox_builder.db.engine import run_async
    from obox_builder.db.session import session_scope

    if format not in {"ndjson", "csv"}:
        raise CommandError("--format must be 'ndjson' or 'csv'.")
    actor_id: int | None = None
    actor_label = actor
    if actor.isdigit():
        actor_id, actor_label = int(actor), ""

    since_day = export.parse_day(since) if since else None
    if since and since_day is None:
        raise CommandError(f"Invalid --since date {since!r}; expected YYYY-MM-DD.")
    until_day = export.parse_day(until) if until else None
    if until and until_day is None:
        raise CommandError(f"Invalid --until date {until!r}; expected YYYY-MM-DD.")

    filters = FeedFilters(
        model_labels=frozenset({model}) if model else None,
        actor=actor_label,
        since=since_day,
        until=until_day + timedelta(days=1) if until_day is not None else None,
    )
    render = export.csv_line if format == "csv" else export.ndjson_line

    async def run() -> int:
        count = 0
        with contextlib.ExitStack() as stack:
            target = (
                stack.enter_context(open(output, "w", encoding="utf-8", newline=""))
                if output
                else typer.get_text_stream("stdout")
            )
            if format == "csv":
                target.write(export.csv_header())
            async with session_scope() as db:
                async for entry in export.export_entries(db, filters, actor_id=actor_id):
                    target.write(render(entry))
                    count += 1
        return count

    obox_builder.setup()
    count = run_async(run())
    export.logger.info(
        "history_export: exported %d row(s) (format=%s, model=%s) to %s.",
        count,
        format,
        model or "*",
        output or "stdout",
    )
    utils.echo(f"Exported {count} row(s) to {output or 'stdout'}.", err=True)
