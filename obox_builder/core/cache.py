"""Async cache API with local-memory and optional Redis backends."""

from __future__ import annotations

import copy
import importlib
import inspect
import json
import math
import threading
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any, Protocol, TypeAlias

from obox_builder.conf import settings
from obox_builder.core.exceptions import ImproperlyConfigured


class _DefaultTimeout:
    pass


DEFAULT_TIMEOUT = _DefaultTimeout()
_MISSING = object()
CacheTimeout: TypeAlias = int | float | timedelta | None | _DefaultTimeout


class CacheBackend(Protocol):
    async def get(self, key: str, default: Any = None) -> Any: ...

    async def set(self, key: str, value: Any, timeout: float | None = None) -> None: ...

    async def add(self, key: str, value: Any, timeout: float | None = None) -> bool: ...

    async def delete(self, key: str) -> bool: ...

    async def clear(self, prefix: str) -> None: ...


class MemoryCache:
    """Process-local cache with monotonic expirations; use Redis across workers."""

    def __init__(self) -> None:
        self._values: dict[str, tuple[Any, float | None]] = {}
        self._lock = threading.RLock()

    async def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            entry = self._values.get(key)
            if entry is None:
                return default
            value, expires_at = entry
            if expires_at is not None and expires_at <= time.monotonic():
                self._values.pop(key, None)
                return default
            return copy.deepcopy(value)

    async def set(self, key: str, value: Any, timeout: float | None = None) -> None:
        with self._lock:
            if timeout is not None and timeout <= 0:
                self._values.pop(key, None)
                return
            expires_at = None if timeout is None else time.monotonic() + timeout
            self._values[key] = (copy.deepcopy(value), expires_at)

    async def add(self, key: str, value: Any, timeout: float | None = None) -> bool:
        with self._lock:
            entry = self._values.get(key)
            if entry is not None:
                _value, expires_at = entry
                if expires_at is None or expires_at > time.monotonic():
                    return False
            if timeout is not None and timeout <= 0:
                self._values.pop(key, None)
                return True
            expires_at = None if timeout is None else time.monotonic() + timeout
            self._values[key] = (copy.deepcopy(value), expires_at)
            return True

    async def delete(self, key: str) -> bool:
        with self._lock:
            return self._values.pop(key, None) is not None

    async def clear(self, prefix: str) -> None:
        with self._lock:
            for key in tuple(self._values):
                if key.startswith(prefix):
                    self._values.pop(key, None)


class RedisCache:
    """Redis-backed cache; install the ``redis-cache`` extra to use it."""

    def __init__(self, url: str | None = None, *, client: Any = None) -> None:
        if client is None:
            try:
                redis_asyncio = importlib.import_module("redis.asyncio")
            except ImportError as exc:
                raise ImproperlyConfigured(
                    "RedisCache requires the optional dependency; "
                    "install obox-builder[redis-cache]."
                ) from exc
            client = redis_asyncio.Redis.from_url(
                url or settings.CACHE_URL,
                decode_responses=False,
            )
        self._client = client

    async def get(self, key: str, default: Any = None) -> Any:
        value = await self._client.get(key)
        return default if value is None else json.loads(value)

    async def set(self, key: str, value: Any, timeout: float | None = None) -> None:
        if timeout is not None and timeout <= 0:
            await self._client.delete(key)
            return
        options = {} if timeout is None else {"px": max(1, math.ceil(timeout * 1000))}
        await self._client.set(key, _encode(value), **options)

    async def add(self, key: str, value: Any, timeout: float | None = None) -> bool:
        options: dict[str, Any] = {"nx": True}
        if timeout is not None:
            options["px"] = max(1, math.ceil(timeout * 1000))
        return bool(await self._client.set(key, _encode(value), **options))

    async def delete(self, key: str) -> bool:
        return bool(await self._client.delete(key))

    async def clear(self, prefix: str) -> None:
        async for key in self._client.scan_iter(match=f"{prefix}*"):
            await self._client.delete(key)

    async def aclose(self) -> None:
        await self._client.aclose()


class DefaultCache:
    """Cache facade that loads the configured backend and supplies namespacing."""

    def __init__(self) -> None:
        self._backend_path = ""
        self._backend: CacheBackend | None = None
        self._lock = threading.Lock()

    def _get_backend(self) -> CacheBackend:
        path = settings.CACHE_BACKEND
        with self._lock:
            if path != self._backend_path or self._backend is None:
                module_path, separator, name = path.rpartition(".")
                if not separator:
                    raise ImproperlyConfigured("CACHE_BACKEND must be a dotted path.")
                try:
                    backend_class = getattr(importlib.import_module(module_path), name)
                    backend = backend_class()
                except (ImportError, AttributeError, TypeError) as exc:
                    raise ImproperlyConfigured(
                        f"Cannot load cache backend {path!r}: {exc}"
                    ) from exc
                required = ("get", "set", "add", "delete", "clear")
                if not all(callable(getattr(backend, method, None)) for method in required):
                    raise ImproperlyConfigured(
                        f"Cache backend {path!r} must define "
                        "get(), set(), add(), delete() and clear()."
                    )
                self._backend_path = path
                self._backend = backend
            if self._backend is None:
                raise ImproperlyConfigured(f"Cache backend {path!r} could not be initialized.")
            return self._backend

    def _key(self, key: str) -> str:
        if not isinstance(key, str) or not key:
            raise ValueError("Cache keys must be non-empty strings.")
        return f"{settings.CACHE_KEY_PREFIX}:{key}"

    @staticmethod
    def _timeout(timeout: CacheTimeout) -> float | None:
        if isinstance(timeout, _DefaultTimeout):
            return float(settings.CACHE_DEFAULT_TIMEOUT)
        if isinstance(timeout, timedelta):
            return timeout.total_seconds()
        if timeout is None:
            return None
        return float(timeout)

    async def get(self, key: str, default: Any = None) -> Any:
        return await self._get_backend().get(self._key(key), default)

    async def set(self, key: str, value: Any, timeout: CacheTimeout = DEFAULT_TIMEOUT) -> None:
        await self._get_backend().set(self._key(key), value, self._timeout(timeout))

    async def add(self, key: str, value: Any, timeout: CacheTimeout = DEFAULT_TIMEOUT) -> bool:
        return await self._get_backend().add(self._key(key), value, self._timeout(timeout))

    async def delete(self, key: str) -> bool:
        return await self._get_backend().delete(self._key(key))

    async def clear(self) -> None:
        await self._get_backend().clear(f"{settings.CACHE_KEY_PREFIX}:")

    async def get_or_set(
        self,
        key: str,
        default: Any | Callable[[], Any],
        timeout: CacheTimeout = DEFAULT_TIMEOUT,
    ) -> Any:
        value = await self.get(key, _MISSING)
        if value is not _MISSING:
            return value
        value = default() if callable(default) else default
        if inspect.isawaitable(value):
            value = await value
        await self.add(key, value, timeout)
        return await self.get(key, value)

    async def aclose(self) -> None:
        close = getattr(self._get_backend(), "aclose", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result


def _encode(value: Any) -> str:
    try:
        return json.dumps(value, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise TypeError("RedisCache values must be JSON-serializable.") from exc


cache = DefaultCache()
