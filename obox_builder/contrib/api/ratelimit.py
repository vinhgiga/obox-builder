"""Bounded in-process token buckets; deployments may inject a shared backend."""

from __future__ import annotations

import math
import time
from typing import Protocol

from obox_builder.contrib.api.problems import Problem


class RateLimiter(Protocol):
    async def check(self, key: str, limit: int) -> dict[str, str]: ...


class MemoryRateLimiter:
    def __init__(self) -> None:
        self.buckets: dict[str, tuple[float, float]] = {}

    async def check(self, key: str, limit: int) -> dict[str, str]:
        now = time.monotonic()
        if key not in self.buckets and len(self.buckets) >= 10_000:
            self.buckets = {
                key: value for key, value in self.buckets.items() if now - value[1] < 60
            }
            if len(self.buckets) >= 10_000:
                raise Problem(429, "rate_limited", "Rate limiter is at capacity.", retry_after=60)
        tokens, stamp = self.buckets.get(key, (float(limit), now))
        tokens = min(float(limit), tokens + (now - stamp) * limit / 60)
        if tokens < 1:
            raise Problem(
                429,
                "rate_limited",
                "Too many requests.",
                retry_after=max(1, math.ceil((1 - tokens) * 60 / limit)),
            )
        self.buckets[key] = (tokens - 1, now)
        reset = math.ceil((limit - tokens + 1) * 60 / limit)
        return {
            "RateLimit": f'"default";r={int(tokens - 1)};t={reset}',
            "RateLimit-Policy": f'"default";q={limit};w=60',
        }
