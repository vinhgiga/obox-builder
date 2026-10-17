from __future__ import annotations

import typer
import uvicorn

from obox_builder.conf import settings
from obox_builder.core.management import utils
from obox_builder.core.management.base import CommandError, command
from obox_builder.utils import import_string

LOG_LEVELS = {0: "warning", 1: "info", 2: "debug", 3: "debug"}


def _as_import_string(target: str) -> str:
    module, _, attribute = target.rpartition(".")
    if not module or not attribute:
        raise CommandError(
            "ASGI_APPLICATION must be a dotted path like 'config.asgi.application', "
            f"got {target!r}."
        )
    return f"{module}:{attribute}"


@command(help="Run the development server.", requires_checks=True)
def handle(
    host: str = typer.Option("127.0.0.1", help="Host to bind."),
    port: int = typer.Option(8000, help="Port to bind."),
    reload: bool = typer.Option(False, "--reload", help="Restart on code changes."),
) -> None:
    target = settings.ASGI_APPLICATION
    if not target:
        raise CommandError("ASGI_APPLICATION is not set (for example 'config.asgi.application').")
    utils.echo(f"Starting development server at http://{host}:{port}/")
    utils.echo("Quit the server with CONTROL-C.")
    options = {
        "host": host,
        "port": port,
        "log_level": LOG_LEVELS.get(utils.get_verbosity(), "info"),
    }
    if reload:
        uvicorn.run(_as_import_string(target), reload=True, **options)
    else:
        uvicorn.run(import_string(target), **options)
