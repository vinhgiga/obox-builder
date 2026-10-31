"""Explicit task imports and a standalone database worker."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
from importlib import import_module
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from obox_builder.conf import load_settings
from obox_builder.contrib.jobs.models import Task, TaskAttempt, utcnow
from obox_builder.contrib.jobs.services import enqueue, retry, revoke
from obox_builder.contrib.jobs.worker import Worker, shutdown


def add_parser(commands: Any) -> None:
    parser = commands.add_parser("jobs", help="Run and manage database background jobs.")
    parser.add_argument("--settings", help="Settings module (detected from the project by default)")
    parser.add_argument(
        "--module", action="append", help="Task module to import (detected by default)"
    )
    actions = parser.add_subparsers(dest="action", required=True)
    worker = actions.add_parser("worker")
    worker.add_argument("--queue", action="append", dest="queues")
    worker.add_argument("--concurrency", type=int, default=1)
    worker.add_argument("--once", action="store_true")
    send = actions.add_parser("enqueue")
    send.add_argument("name")
    send.add_argument("--args", default="[]", help="JSON positional arguments")
    send.add_argument("--kwargs", default="{}", help="JSON keyword arguments")
    for name in ("status", "retry", "revoke"):
        actions.add_parser(name).add_argument("id")
    purge = actions.add_parser("purge")
    purge.add_argument("--days", type=int, default=30)


async def run(arguments: argparse.Namespace) -> None:
    project_root = str(Path.cwd().resolve())
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    settings_module = (
        arguments.settings
        or os.environ.get("OBOX_SETTINGS_MODULE")
        or ("app.settings" if Path("app/settings.py").is_file() else "setting")
    )
    os.environ["OBOX_SETTINGS_MODULE"] = settings_module
    sessions = load_settings(settings_module).session_factory
    try:
        await _run(arguments, sessions)
    finally:
        bind = sessions.kw.get("bind")
        if isinstance(bind, AsyncEngine):
            await bind.dispose()


async def _run(arguments: argparse.Namespace, sessions: async_sessionmaker[AsyncSession]) -> None:
    settings_module = arguments.settings or os.environ["OBOX_SETTINGS_MODULE"]
    package, _, _ = settings_module.rpartition(".")
    task_module = f"{package}.jobs.tasks" if package else "tasks"
    task_path = Path(*task_module.split("."))
    modules = arguments.module or (
        [task_module]
        if task_path.with_suffix(".py").is_file() or (task_path / "__init__.py").is_file()
        else []
    )
    for module in modules:
        import_module(module)
    if arguments.action == "worker":
        worker = Worker(
            sessions,
            queues=tuple(arguments.queues or ["default"]),
            concurrency=arguments.concurrency,
        )
        if arguments.once:
            await worker.run_once()
            return
        loop = asyncio.get_running_loop()
        stopping = asyncio.Event()
        previous = {}
        try:
            for sig in (signal.SIGINT, signal.SIGTERM):
                previous[sig] = signal.signal(
                    sig,
                    lambda *_: loop.call_soon_threadsafe(stopping.set),
                )
            running = asyncio.create_task(worker.run())
            requested = asyncio.create_task(stopping.wait())
            try:
                await asyncio.wait((running, requested), return_when=asyncio.FIRST_COMPLETED)
            finally:
                requested.cancel()
                await asyncio.gather(requested, return_exceptions=True)
                await shutdown(worker, running)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
        return
    async with sessions.begin() as db:
        if arguments.action == "enqueue":
            row = await enqueue(
                db,
                arguments.name,
                args=json.loads(arguments.args),
                kwargs=json.loads(arguments.kwargs),
            )
            print(row.id)
        elif arguments.action == "status":
            record = await db.get(Task, arguments.id)
            if record is None:
                raise ValueError("Task not found.")
            print(
                json.dumps(
                    {column.key: getattr(record, column.key) for column in Task.__table__.columns},
                    default=str,
                )
            )
        elif arguments.action in {"retry", "revoke"}:
            operation = retry if arguments.action == "retry" else revoke
            print(await operation(db, arguments.id))
        else:
            from datetime import timedelta

            if arguments.days < 0:
                raise ValueError("Retention days must be nonnegative.")
            ids = list(
                await db.scalars(
                    sa.select(Task.id)
                    .where(Task.date_done < utcnow() - timedelta(days=arguments.days))
                    .with_for_update(skip_locked=True)
                )
            )
            await db.execute(sa.delete(TaskAttempt).where(TaskAttempt.task_id.in_(ids)))
            await db.execute(sa.delete(Task).where(Task.id.in_(ids)))
            print(len(ids))
