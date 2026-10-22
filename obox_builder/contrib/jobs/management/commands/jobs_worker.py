from __future__ import annotations

import asyncio
import signal
import sys
from contextlib import suppress
from types import FrameType

import typer

from obox_builder.core.management.base import command


@command(help="Run native task processes; Ctrl+C drains running tasks.", requires_checks=True)
async def handle(
    queue: list[str] = typer.Option(["default"], "--queue", help="Repeat for multiple queues."),
    concurrency: int = typer.Option(
        1, "--concurrency", min=1, help="Maximum active task processes."
    ),
    once: bool = typer.Option(False, "--once", help="Process at most one task and exit."),
) -> None:
    from obox_builder.contrib.jobs.worker import Worker, shutdown

    worker = Worker(queues=tuple(queue), concurrency=concurrency)
    if once:
        await worker.run_once()
        return
    loop = asyncio.get_running_loop()
    stopping = asyncio.Event()

    def stop(_number: int, _frame: FrameType | None) -> None:
        loop.call_soon_threadsafe(stopping.set)

    signals = [signal.SIGINT, signal.SIGTERM]
    if sys.platform == "win32":
        signals.append(signal.SIGBREAK)
    previous = {sig: signal.getsignal(sig) for sig in signals}
    task = asyncio.create_task(worker.run())
    waiter = asyncio.create_task(stopping.wait())
    try:
        # signal.signal works with the default Windows Proactor loop too.
        for sig in signals:
            signal.signal(sig, stop)
        await asyncio.wait((task, waiter), return_when=asyncio.FIRST_COMPLETED)
        await shutdown(worker, task)
    finally:
        waiter.cancel()
        with suppress(asyncio.CancelledError):
            await waiter
        if not task.done():
            await shutdown(worker, task)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
