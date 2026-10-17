from __future__ import annotations

import typer

from obox_builder.core.management import utils
from obox_builder.core.management.base import CommandError, command
from obox_builder.staticfiles import collect_static


@command(help="Collect static files into STATIC_ROOT.", requires_settings=True)
def handle(
    clear: bool = typer.Option(False, "--clear", help="Delete STATIC_ROOT before collecting."),
    dry_run: bool = typer.Option(False, "--dry-run", help="List files without copying."),
) -> None:
    try:
        result = collect_static(clear=clear, dry_run=dry_run)
    except Exception as exc:  # ImproperlyConfigured and friends
        raise CommandError(str(exc)) from exc

    prefix = "[dry-run] Would copy" if dry_run else "Copied"
    utils.echo(f"{prefix} {len(result.copied)} file(s).")
    for name in result.copied:
        utils.echo(f"  {name}", level=2)
    if result.duplicates:
        utils.echo(f"Skipped {len(result.duplicates)} duplicate file(s).", level=1)
        for name in result.duplicates:
            utils.echo(f"  {name}", level=2)
    if result.cleared:
        utils.echo("Cleared STATIC_ROOT.")
