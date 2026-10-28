from __future__ import annotations

import re

import typer
import uvicorn

from obox_builder.conf import settings
from obox_builder.core.management import utils
from obox_builder.core.management.base import CommandError, command

LOG_LEVELS = {0: "warning", 1: "info", 2: "debug", 3: "debug"}
ADDRPORT = re.compile(r"(?:(\[[a-fA-F0-9:]+\]|[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)*):)?([0-9]+)")


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
    addrport: str | None = typer.Argument(
        None, help="Port number or address:port (default: 127.0.0.1:8000)."
    ),
    host: str | None = typer.Option(None, help="Host to bind."),
    port: int | None = typer.Option(None, min=0, max=65535, help="Port to bind."),
    reload: bool = typer.Option(
        True, "--reload/--noreload", help="Restart on code changes (enabled by default)."
    ),
) -> None:
    if addrport is not None:
        if host is not None or port is not None:
            raise CommandError("The address argument cannot be combined with --host or --port.")
        match = ADDRPORT.fullmatch(addrport)
        if match is None:
            raise CommandError(f'"{addrport}" is not a valid port number or address:port pair.')
        host, raw_port = match.groups()
        port = int(raw_port)
    host = "127.0.0.1" if host is None else host
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if host == "0":
        host = "0.0.0.0"  # noqa: S104 - explicitly requested by the address argument
    port = 8000 if port is None else port
    if not 0 <= port <= 65535:
        raise CommandError("Port must be between 0 and 65535.")
    target = settings.ASGI_APPLICATION
    if not target:
        raise CommandError("ASGI_APPLICATION is not set (for example 'config.asgi.application').")
    application = _as_import_string(target)
    display_host = f"[{host}]" if ":" in host else host
    utils.echo(f"Starting development server at http://{display_host}:{port}/")
    utils.echo("Quit the server with CONTROL-C.")
    uvicorn.run(
        application,
        host=host,
        port=port,
        reload=reload,
        log_level=LOG_LEVELS.get(utils.get_verbosity(), "info"),
    )
