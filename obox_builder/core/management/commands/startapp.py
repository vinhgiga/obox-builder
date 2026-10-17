from __future__ import annotations

from pathlib import Path

import typer

from obox_builder.core.management import utils
from obox_builder.core.management.base import command
from obox_builder.core.management.templates import (
    APP_TEMPLATE_DIR,
    ensure_target_available,
    module_path_for,
    render_tree,
    validate_name,
)


@command(help="Create a new app skeleton.", requires_settings=False)
def handle(
    name: str = typer.Argument(..., help="App name (a Python identifier)."),
    directory: str | None = typer.Argument(None, help="Target directory (default: ./NAME)."),
) -> None:
    validate_name(name, kind="app")
    target = Path(directory) if directory else Path(name)
    module = module_path_for(directory, fallback=name) if directory else name
    ensure_target_available(target)
    context = {
        "app_name": name,
        "app_label": target.name,
        "app_module": module,
        "app_class": "".join(part.capitalize() for part in target.name.split("_")) + "Config",
    }
    created = render_tree(APP_TEMPLATE_DIR, target, context)
    utils.echo(f"Created app {name!r} in {target}.")
    for path in created:
        utils.echo(f"  {path.relative_to(target)}", level=2)
    utils.echo(f"Add {module!r} to INSTALLED_APPS in your settings module.")
