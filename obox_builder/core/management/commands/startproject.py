from __future__ import annotations

import secrets
from pathlib import Path

import typer

from obox_builder import __version__
from obox_builder.core.management import utils
from obox_builder.core.management.base import command
from obox_builder.core.management.templates import (
    PROJECT_TEMPLATE_DIR,
    ensure_target_available,
    render_tree,
    validate_name,
)


@command(help="Create a new obox-builder project skeleton.", requires_settings=False)
def handle(
    name: str = typer.Argument(..., help="Project name (a Python identifier)."),
    directory: str | None = typer.Argument(None, help="Target directory (default: ./NAME)."),
) -> None:
    validate_name(name, kind="project")
    target = Path(directory) if directory else Path(name)
    ensure_target_available(target)
    context = {
        "project_name": name,
        "secret_key": secrets.token_urlsafe(48),
        "obox_version": __version__,
    }
    created = render_tree(PROJECT_TEMPLATE_DIR, target, context)
    utils.echo(f"Created project {name!r} in {target}.")
    for path in created:
        utils.echo(f"  {path.relative_to(target)}", level=2)
    utils.echo("Next steps:")
    utils.echo(f"  cd {target}")
    utils.echo("  python manage.py runserver")
