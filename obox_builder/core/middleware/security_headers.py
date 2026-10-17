"""Baseline security headers for every response (see plan §8.6)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]

DEFAULT_HEADERS: dict[str, str] = {
    "x-content-type-options": "nosniff",
    "referrer-policy": "same-origin",
    "x-frame-options": "DENY",
}


class SecurityHeadersMiddleware:
    """Pure ASGI middleware appending default security headers when missing."""

    def __init__(self, app: Any, headers: dict[str, str] | None = None) -> None:
        self.app = app
        merged = {**DEFAULT_HEADERS, **(headers or {})}
        self.headers = [
            (key.lower().encode("latin-1"), value.encode("latin-1"))
            for key, value in merged.items()
        ]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                existing = {name.lower() for name, _ in message.get("headers", [])}
                headers = message.setdefault("headers", [])
                headers.extend(
                    (name, value) for name, value in self.headers if name not in existing
                )
            await send(message)

        await self.app(scope, receive, send_with_headers)
