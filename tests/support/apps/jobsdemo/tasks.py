from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from obox_builder.contrib.jobs import current_task, task


@task(name="jobs.cpu", max_retries=1)
def cpu(n: int = 25_000) -> dict[str, Any]:
    request = current_task()
    return {
        "sum": sum(i * i for i in range(n)),
        "pid": os.getpid(),
        "id": request.id,
        "retries": request.retries,
        "worker": request.hostname,
    }


@task(name="jobs.async")
async def asynchronous(value: str) -> str:
    await asyncio.sleep(0)
    return value


@task(name="jobs.transient", max_retries=1)
def transient() -> int:
    if current_task().retries == 0:
        raise TimeoutError("private-error-message")
    return current_task().retries


@task(name="jobs.failure", max_retries=1)
def failure() -> None:
    raise ValueError("private-error-message")


@task(name="jobs.no_retry", autoretry_for=())
def no_retry() -> None:
    raise ValueError("private-error-message")


@task(name="jobs.invalid_result", max_retries=0)
def invalid_result() -> object:
    return object()


@task(name="jobs.timeout", max_retries=0, time_limit=3)
def timeout(marker: str) -> None:
    Path(marker).write_text(str(os.getpid()), encoding="utf-8")
    while True:
        pass


@task(name="jobs.gated", time_limit=20)
def gated(marker: str, release: str) -> int:
    Path(marker).write_text(str(os.getpid()), encoding="utf-8")
    gate = Path(release)
    while not gate.exists():
        pass
    return os.getpid()
