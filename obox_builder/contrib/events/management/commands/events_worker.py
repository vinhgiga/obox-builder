from __future__ import annotations

import asyncio
import signal

import typer

from obox_builder.core.management.base import command


@command(
    help="Run the durable event worker (SIGTERM/SIGINT drain the current handler).",
    requires_checks=True,
)
async def handle(
    queue: list[str] = typer.Option(["default"], "--queue", help="Repeat for multiple queues."),
    once: bool = typer.Option(False, "--once", help="Process at most one delivery and exit."),
) -> None:
    from obox_builder.contrib.events.worker import Worker, shutdown

    worker = Worker(queues=tuple(queue))
    if once:
        await worker.run_once()
        return
    task = asyncio.create_task(worker.run())
    loop = asyncio.get_running_loop()
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    stopping = asyncio.Event()
    for sig in previous:
        loop.add_signal_handler(sig, stopping.set)
    waiter = asyncio.create_task(stopping.wait())
    try:
        await asyncio.wait((task, waiter), return_when=asyncio.FIRST_COMPLETED)
        await shutdown(worker, task)
    finally:
        waiter.cancel()
        for sig, handler in previous.items():
            loop.remove_signal_handler(sig)
            signal.signal(sig, handler)
