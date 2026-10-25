from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from obox_builder.conf import BaseSettings, settings
from obox_builder.core.cache import DefaultCache, MemoryCache, RedisCache, cache
from pydantic import SecretStr


@pytest.fixture
def configured_cache() -> Iterator[DefaultCache]:
    original = settings._wrapped
    settings.configure(
        BaseSettings(
            SECRET_KEY=SecretStr("s" * 40),
            DATABASE_URL="sqlite+aiosqlite://",
            CACHE_BACKEND="obox_builder.core.cache.MemoryCache",
            CACHE_KEY_PREFIX="test-cache",
            CACHE_DEFAULT_TIMEOUT=30,
        )
    )
    try:
        yield DefaultCache()
    finally:
        settings._wrapped = original


class RedisStub:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.options: dict[str, Any] = {}
        self.closed = False

    async def get(self, key: str) -> bytes | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, **options: Any) -> bool:
        self.options = options
        if options.get("nx") and key in self.values:
            return False
        self.values[key] = value.encode()
        return True

    async def delete(self, key: str) -> int:
        return int(self.values.pop(key, None) is not None)

    async def scan_iter(self, *, match: str) -> AsyncIterator[str]:
        prefix = match.removesuffix("*")
        for key in tuple(self.values):
            if key.startswith(prefix):
                yield key

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_memory_cache_expires_and_copies_values(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [10.0]
    monkeypatch.setattr("obox_builder.core.cache.time.monotonic", lambda: now[0])
    backend = MemoryCache()
    value = {"items": [1]}

    await backend.set("key", value, timeout=2)
    value["items"].append(2)
    result = await backend.get("key")
    assert result == {"items": [1]}
    result["items"].append(3)
    assert await backend.get("key") == {"items": [1]}

    now[0] += 2
    assert await backend.get("key", "missing") == "missing"


@pytest.mark.asyncio
async def test_cache_facade_namespaces_keys_and_supports_get_or_set(
    configured_cache: DefaultCache,
) -> None:
    calls = 0

    async def make_value() -> dict[str, str]:
        nonlocal calls
        calls += 1
        return {"value": "created"}

    assert await configured_cache.get_or_set("entry", make_value) == {"value": "created"}
    assert await configured_cache.get_or_set("entry", make_value) == {"value": "created"}
    assert calls == 1
    assert await configured_cache.add("entry", "replacement") is False
    assert await configured_cache.delete("entry") is True
    assert await configured_cache.delete("entry") is False


@pytest.mark.asyncio
async def test_clear_only_removes_the_active_namespace() -> None:
    original = settings._wrapped
    settings.configure(
        BaseSettings(
            SECRET_KEY=SecretStr("s" * 40),
            DATABASE_URL="sqlite+aiosqlite://",
            CACHE_KEY_PREFIX="first",
        )
    )
    try:
        await cache.set("key", "one")
        settings.configure(
            BaseSettings(
                SECRET_KEY=SecretStr("s" * 40),
                DATABASE_URL="sqlite+aiosqlite://",
                CACHE_KEY_PREFIX="second",
            )
        )
        await cache.set("key", "two")
        await cache.clear()
        assert await cache.get("key") is None
        settings.configure(
            BaseSettings(
                SECRET_KEY=SecretStr("s" * 40),
                DATABASE_URL="sqlite+aiosqlite://",
                CACHE_KEY_PREFIX="first",
            )
        )
        assert await cache.get("key") == "one"
    finally:
        settings._wrapped = original


@pytest.mark.asyncio
async def test_redis_cache_uses_atomic_add_expiration_and_prefix_clear() -> None:
    client = RedisStub()
    backend = RedisCache(client=client)

    assert await backend.add("group:one", {"ok": True}, timeout=1.2)
    assert client.options == {"nx": True, "px": 1200}
    assert await backend.get("group:one") == {"ok": True}
    assert not await backend.add("group:one", "changed")
    await backend.set("other:one", 1)
    await backend.clear("group:")
    assert await backend.get("group:one") is None
    assert await backend.get("other:one") == 1
    await backend.aclose()
    assert client.closed
