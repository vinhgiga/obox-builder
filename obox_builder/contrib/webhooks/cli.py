"""Run a host-configured webhook worker in its own process."""

import argparse
import asyncio
import signal
import sys
from importlib import import_module
from pathlib import Path
from typing import Any

from obox_builder.application import Obox
from obox_builder.contrib.webhooks import Webhooks
from obox_builder.contrib.webhooks.worker import Worker


def add_parser(commands: Any) -> None:
    parser = commands.add_parser("webhooks", help="Deliver durable change notifications.")
    actions = parser.add_subparsers(dest="action", required=True)
    for name in ("worker", "prune"):
        command = actions.add_parser(name)
        command.add_argument("--app", required=True, help="Host application as module:attribute.")
        command.add_argument("--factory", action="store_true", help="Call the application factory.")
        if name == "worker":
            command.add_argument("--once", action="store_true")


async def run(arguments: argparse.Namespace) -> None:
    sys.path.insert(0, str(Path.cwd()))
    module, separator, name = arguments.app.partition(":")
    if not separator or not module or not name:
        raise ValueError("Use --app module:attribute.")
    app = getattr(import_module(module), name)
    app = app() if arguments.factory else app
    obox = app if isinstance(app, Obox) else getattr(getattr(app, "state", None), "obox", None)
    if not isinstance(obox, Obox):
        raise ValueError("The application must install the Webhooks plugin.")
    plugin = obox.get_plugin("webhooks")
    if not isinstance(plugin, Webhooks):
        raise ValueError("The application must install the Webhooks plugin.")
    async with obox.app.router.lifespan_context(obox.app):
        if arguments.action == "prune":
            async with obox.uow(principal=None) as ctx:
                await plugin.prune(ctx)
            return
        worker = Worker(plugin)
        if arguments.once:
            await worker.run_once()
            return
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            for sig in previous:
                signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
            await worker.run(stop)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
