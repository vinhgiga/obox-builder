"""Registered task functions and the current child-process request."""

from __future__ import annotations

import inspect
import json
import math
import re
from collections.abc import Callable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, overload

from obox_builder.errors import ImproperlyConfigured

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from obox_builder.contrib.jobs.models import Task

NAME_RE = re.compile(r"^[a-zA-Z0-9_][a-zA-Z0-9_.:-]{0,154}$")


@dataclass(frozen=True)
class TaskRequest:
    id: str
    name: str
    retries: int
    hostname: str
    expires: datetime | None = None
    priority: int = 0


_request: ContextVar[TaskRequest] = ContextVar("obox.jobs.request")


def current_task() -> TaskRequest:
    """Return execution metadata; ``id`` is stable across retries."""
    return _request.get()


def encode_json(value: Any) -> str:
    encoded = json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 1_048_576:
        raise ValueError("Task data exceeds 1 MiB.")
    return encoded


@dataclass(frozen=True)
class TaskDefinition:
    func: Callable[..., Any]
    name: str
    queue: str
    max_retries: int
    expires: datetime | None
    priority: int | None
    time_limit: float
    autoretry_for: tuple[type[Exception], ...]

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.func(*args, **kwargs)

    async def delay(self, db: AsyncSession, *args: Any, **kwargs: Any) -> Task:
        return await self.apply_async(db, args=args, kwargs=kwargs)

    async def apply_async(
        self,
        db: AsyncSession,
        *,
        args: Sequence[Any] = (),
        kwargs: dict[str, Any] | None = None,
        eta: datetime | None = None,
        expires: datetime | None = None,
        priority: int | None = None,
        queue: str | None = None,
        dedup_key: str | None = None,
    ) -> Task:
        from obox_builder.contrib.jobs.services import enqueue

        return await enqueue(
            db,
            self.name,
            args=args,
            kwargs=kwargs,
            eta=eta,
            expires=expires,
            priority=priority,
            queue=queue,
            dedup_key=dedup_key,
        )


_tasks: dict[str, TaskDefinition] = {}


@overload
def task(function: Callable[..., Any], /) -> TaskDefinition: ...


@overload
def task(
    *,
    name: str | None = None,
    queue: str = "default",
    max_retries: int = 3,
    expires: datetime | None = None,
    priority: int | None = None,
    time_limit: float = 300.0,
    autoretry_for: tuple[type[Exception], ...] = (Exception,),
) -> Callable[[Callable[..., Any]], TaskDefinition]: ...


def task(
    function: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    queue: str = "default",
    max_retries: int = 3,
    expires: datetime | None = None,
    priority: int | None = None,
    time_limit: float = 300.0,
    autoretry_for: tuple[type[Exception], ...] = (Exception,),
) -> TaskDefinition | Callable[[Callable[..., Any]], TaskDefinition]:
    """Register a sync/async function, imported explicitly by the worker.

    Use ``@task`` or ``@task(...)``; names default to the module and function name.
    ``max_retries`` excludes the initial execution. Exceptions matching
    ``autoretry_for`` retry automatically; use ``()`` to disable auto-retry.
    """
    if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
        raise ImproperlyConfigured("max_retries must be a nonnegative integer.")
    if not math.isfinite(time_limit) or time_limit <= 0:
        raise ImproperlyConfigured("time_limit must be finite and positive.")
    if not isinstance(autoretry_for, tuple) or any(
        not isinstance(exc, type) or not issubclass(exc, Exception) for exc in autoretry_for
    ):
        raise ImproperlyConfigured("autoretry_for must be a tuple of Exception classes.")

    def register(func: Callable[..., Any]) -> TaskDefinition:
        key = name or f"{func.__module__}.{func.__qualname__}"
        if (
            not NAME_RE.fullmatch(key)
            or not inspect.isfunction(func)
            or inspect.isgeneratorfunction(func)
            or inspect.isasyncgenfunction(func)
            or "<locals>" in func.__qualname__
            or func.__module__ == "__main__"
        ):
            raise ImproperlyConfigured("Tasks must be importable module-level functions.")
        existing = _tasks.get(key)
        if existing is not None and existing.func is not func:
            raise ImproperlyConfigured(f"Duplicate task name {key!r}.")
        definition = TaskDefinition(
            func, key, queue, max_retries, expires, priority, time_limit, autoretry_for
        )
        _tasks[key] = definition
        return definition

    return register(function) if function is not None else register


def registered_tasks() -> tuple[TaskDefinition, ...]:
    return tuple(_tasks.values())


def get_task(name: str) -> TaskDefinition:
    try:
        return _tasks[name]
    except KeyError:
        raise ValueError(f"Unknown task {name!r}; import its task module first.") from None
