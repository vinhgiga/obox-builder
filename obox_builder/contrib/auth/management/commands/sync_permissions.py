from __future__ import annotations

import typer

from obox_builder.core.management import utils
from obox_builder.core.management.base import command


@command(help="Create missing permissions from installed apps.", requires_checks=True)
def handle(
    prune: bool = typer.Option(False, "--prune", help="Delete permissions no longer declared."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report without writing."),
) -> None:
    import obox_builder
    from obox_builder.contrib.auth.permissions import sync_permissions
    from obox_builder.db.engine import run_async
    from obox_builder.db.session import session_scope

    async def run() -> None:
        async with session_scope() as db:
            result = await sync_permissions(db, prune=prune, dry_run=dry_run)
        verb = "Would create" if dry_run else "Created"
        utils.echo(f"{verb} {len(result.created)} permission(s).")
        for name in result.created:
            utils.echo(f"  + {name}", level=2)
        utils.echo(f"{len(result.retired)} retired permission(s); existing grants are retained.")
        for name in result.retired:
            utils.echo(f"  ~ {name}", level=2)
        if prune:
            verb = "Would prune" if dry_run else "Pruned"
            utils.echo(f"{verb} {len(result.pruned)} permission(s).")
            for name in result.pruned:
                utils.echo(f"  - {name}", level=2)

    obox_builder.setup()
    run_async(run())
