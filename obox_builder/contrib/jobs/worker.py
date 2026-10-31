"""Competing DB consumers; each execution runs in a fresh Python process."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import socket
import subprocess
import sys
import tempfile
import uuid
from contextlib import suppress
from datetime import UTC, timedelta
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from obox_builder.contrib.jobs.models import Task, TaskAttempt, utcnow
from obox_builder.contrib.jobs.presence import presence
from obox_builder.contrib.jobs.registry import NAME_RE, get_task
from obox_builder.errors import ImproperlyConfigured

logger = logging.getLogger("obox.jobs")


class LeaseLost(RuntimeError):
    """A replacement worker or revoke operation owns the task now."""


def _fence(task: Task) -> Any:
    return sa.and_(
        Task.id == task.id,
        Task.status == "STARTED",
        Task.lock_token == task.lock_token,
        Task.locked_until > utcnow(),
    )


async def claim(
    sessions: async_sessionmaker[AsyncSession],
    queues: tuple[str, ...],
    worker: str,
    lease_seconds: float = 60,
) -> Task | None:
    now = utcnow()
    async with sessions.begin() as db:
        expired = list(
            await db.scalars(
                sa.update(Task)
                .where(
                    Task.queue.in_(queues),
                    sa.or_(
                        Task.status.in_(("PENDING", "RETRY")),
                        sa.and_(Task.status == "STARTED", Task.locked_until <= now),
                    ),
                    Task.expires.is_not(None),
                    Task.expires <= now,
                )
                .values(
                    status="REVOKED",
                    date_done=now,
                    traceback="Expired",
                    lock_token=None,
                    locked_until=None,
                )
                .returning(Task.id)
            )
        )
        if expired:
            await db.execute(
                sa.update(TaskAttempt)
                .where(TaskAttempt.task_id.in_(expired), TaskAttempt.status == "STARTED")
                .values(status="ABANDONED", date_done=now, traceback="Expired")
            )
        exhausted = list(
            (
                await db.scalars(
                    sa.update(Task)
                    .where(
                        Task.queue.in_(queues),
                        Task.status == "STARTED",
                        Task.locked_until <= now,
                        Task.retries >= Task.max_retries,
                    )
                    .values(
                        status="FAILURE",
                        date_done=now,
                        lock_token=None,
                        locked_until=None,
                        traceback="LeaseExpired",
                    )
                    .returning(Task.id)
                )
            ).all()
        )
        if exhausted:
            await db.execute(
                sa.update(TaskAttempt)
                .where(TaskAttempt.task_id.in_(exhausted), TaskAttempt.status == "STARTED")
                .values(status="ABANDONED", date_done=now, traceback="LeaseExpired")
            )
        eligible = sa.or_(
            sa.and_(
                Task.status.in_(("PENDING", "RETRY")),
                Task.eta <= now,
                sa.or_(Task.expires.is_(None), Task.expires > now),
            ),
            sa.and_(
                Task.status == "STARTED", Task.locked_until <= now, Task.retries < Task.max_retries
            ),
        )
        candidate = (
            sa.select(Task.id)
            .where(Task.queue.in_(queues), eligible)
            .order_by(Task.priority.desc(), Task.eta, Task.date_created, Task.id)
            .limit(1)
            .with_for_update(skip_locked=True)
            .scalar_subquery()
        )
        row = (
            await db.scalars(
                sa.update(Task)
                .where(Task.id == candidate)
                .values(
                    status="STARTED",
                    retries=sa.case(
                        (Task.status == "STARTED", Task.retries + 1), else_=Task.retries
                    ),
                    worker=worker,
                    lock_token=uuid.uuid4(),
                    locked_until=now + timedelta(seconds=lease_seconds),
                )
                .returning(Task)
                .execution_options(synchronize_session=False)
            )
        ).one_or_none()
        if row is not None:
            await db.execute(
                sa.update(TaskAttempt)
                .where(TaskAttempt.task_id == row.id, TaskAttempt.status == "STARTED")
                .values(status="ABANDONED", date_done=now, traceback="LeaseExpired")
            )
            db.add(TaskAttempt(task_id=row.id, retries=row.retries, worker=worker))
        return row


async def _heartbeat(
    sessions: async_sessionmaker[AsyncSession],
    task: Task,
    lease_seconds: float,
) -> None:
    while True:
        await asyncio.sleep(lease_seconds / 3)
        async with sessions.begin() as db:
            owned = await db.scalar(
                sa.update(Task)
                .where(_fence(task))
                .values(locked_until=utcnow() + timedelta(seconds=lease_seconds))
                .returning(Task.id)
                .execution_options(synchronize_session=False)
            )
            if owned is None:
                raise LeaseLost(task.id)


async def _execute(task: Task) -> dict[str, Any]:
    definition = get_task(task.name)
    expires = task.expires
    if expires is not None and expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    data = {
        "module": definition.func.__module__,
        "task_id": task.id,
        "name": task.name,
        "args": task.args,
        "kwargs": task.kwargs,
        "retries": task.retries,
        "worker": task.worker,
        "expires": expires.isoformat() if expires is not None else None,
        "priority": task.priority,
    }
    # Private files avoid exposing arguments in process listings or mixing
    # task stdout with the result protocol. The directory outlives the child.
    with tempfile.TemporaryDirectory(prefix="obox-job-") as directory:
        input_path, output_path = Path(directory) / "input.json", Path(directory) / "result.json"
        input_path.write_text(json.dumps(data, allow_nan=False), encoding="utf-8")
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "obox_builder.contrib.jobs.runner",
            str(input_path),
            str(output_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=sys.platform != "win32",
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            # Management commands may add the project directory to sys.path.
            env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
        )
        try:
            await process.wait()
            if process.returncode != 0 or not output_path.exists():
                return {"status": "FAILURE", "traceback": "WorkerLost", "retryable": True}
            with output_path.open("rb") as output:
                encoded = output.read(1_048_576 + 32_769)
            if len(encoded) > 1_048_576 + 32_768:
                raise ValueError("Child result exceeds the task data limit.")
            response: dict[str, Any] = json.loads(encoded)
            return response
        finally:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
                await process.wait()


async def _finish(
    sessions: async_sessionmaker[AsyncSession],
    task: Task,
    response: dict[str, Any],
) -> None:
    now = utcnow()
    succeeded = response["status"] == "SUCCESS"
    retrying = (
        not succeeded and response.get("retryable", False) and task.retries < task.max_retries
    )
    status = "SUCCESS" if succeeded else ("RETRY" if retrying else "FAILURE")
    delay = random.uniform(0, min(300, 2 ** min(task.retries, 30)))  # noqa: S311
    async with sessions.begin() as db:
        owned = await db.scalar(
            sa.update(Task)
            .where(_fence(task))
            .values(
                status=status,
                result=response.get("result") if succeeded else None,
                traceback=response.get("traceback"),
                retries=task.retries + 1 if retrying else task.retries,
                eta=now + timedelta(seconds=delay) if retrying else Task.eta,
                date_done=None if retrying else now,
                lock_token=None,
                locked_until=None,
            )
            .returning(Task.id)
            .execution_options(synchronize_session=False)
        )
        if owned is None:
            raise LeaseLost(task.id)
        await db.execute(
            sa.update(TaskAttempt)
            .where(TaskAttempt.task_id == task.id, TaskAttempt.retries == task.retries)
            .values(
                status="SUCCESS" if succeeded else "FAILURE",
                date_done=now,
                traceback=response.get("traceback"),
            )
        )


async def execute(
    sessions: async_sessionmaker[AsyncSession],
    task: Task,
    lease_seconds: float = 60,
) -> None:
    async with sessions.begin() as db:
        if await db.scalar(sa.select(Task.id).where(_fence(task))) is None:
            return
    try:
        definition = get_task(task.name)
    except ValueError:
        await _finish(
            sessions, task, {"status": "FAILURE", "traceback": "NotRegistered", "retryable": False}
        )
        return
    heartbeat = asyncio.create_task(_heartbeat(sessions, task, lease_seconds))
    execution = asyncio.create_task(_execute(task))
    try:
        async with asyncio.timeout(definition.time_limit):
            done, _ = await asyncio.wait(
                (heartbeat, execution), return_when=asyncio.FIRST_COMPLETED
            )
            if heartbeat in done:
                await heartbeat  # Lease loss or a DB error must stop the child.
            response = await execution
    except LeaseLost:
        logger.info("Task %s lost its lease; execution stopped.", task.id)
        return
    except Exception as exc:
        response = {"status": "FAILURE", "traceback": type(exc).__name__, "retryable": True}
    finally:
        execution.cancel()
        heartbeat.cancel()
        for pending in (execution, heartbeat):
            with suppress(asyncio.CancelledError, Exception):
                await pending
    try:
        await _finish(sessions, task, response)
        logger.info("Task %s attempt %s: %s.", task.id, task.retries, response["status"])
    except LeaseLost:
        logger.info("Task %s lost its lease; result ignored.", task.id)


class Worker:
    """Bounded process concurrency. Multiple workers may share the same queues."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        queues: tuple[str, ...] = ("default",),
        concurrency: int = 1,
        lease_seconds: float = 60,
        poll_interval: float = 1,
    ) -> None:
        if not queues or any(not NAME_RE.fullmatch(queue) for queue in queues):
            raise ImproperlyConfigured("Select at least one valid queue.")
        if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1:
            raise ImproperlyConfigured("Worker concurrency must be a positive integer.")
        if any(not math.isfinite(value) or value <= 0 for value in (lease_seconds, poll_interval)):
            raise ImproperlyConfigured("Worker intervals must be finite and positive.")
        bind = sessions.kw.get("bind")
        if sys.platform == "win32" and bind is not None and bind.dialect.driver == "psycopg":
            raise ImproperlyConfigured("Windows job workers require asyncpg or aiosqlite.")
        self.sessions = async_sessionmaker(
            class_=sessions.class_,
            **{**sessions.kw, "expire_on_commit": False},
        )
        self.lease_seconds = lease_seconds
        self.poll_interval = poll_interval
        self.queues = queues
        self.concurrency = concurrency
        self.hostname = f"{socket.gethostname()[:100]}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._stopping = asyncio.Event()

    async def run_once(self) -> bool:
        self._check_loop()
        async with presence(self.sessions, self.hostname, self.queues, self.concurrency):
            return await self._run_once()

    async def _run_once(self) -> bool:
        row = await claim(self.sessions, self.queues, self.hostname, self.lease_seconds)
        if row is None:
            return False
        await execute(self.sessions, row, self.lease_seconds)
        return True

    def stop(self) -> None:
        self._stopping.set()

    async def _consume(self) -> None:
        while not self._stopping.is_set():
            try:
                if await self._run_once():
                    continue
            except Exception as exc:
                logger.error("Background worker operation failed: %s.", type(exc).__name__)
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=self.poll_interval)

    async def run(self) -> None:
        self._check_loop()
        async with (
            presence(self.sessions, self.hostname, self.queues, self.concurrency),
            asyncio.TaskGroup() as group,
        ):
            for _ in range(self.concurrency):
                group.create_task(self._consume())

    @staticmethod
    def _check_loop() -> None:
        if sys.platform == "win32" and isinstance(
            asyncio.get_running_loop(), asyncio.SelectorEventLoop
        ):
            raise ImproperlyConfigured(
                "Windows job workers require the default Proactor event loop."
            )


async def shutdown(worker: Worker, task: asyncio.Task[None], timeout: float = 30) -> None:
    worker.stop()
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
    except TimeoutError:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
