from __future__ import annotations

import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

X_REQUEST_ID_HEADER = "x-request-id"


class RequestIDMiddleware:
    """Pure ASGI middleware attaching a request id to scope state and response headers."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        scope_headers = dict(scope.get("headers", []))
        incoming = scope_headers.get(X_REQUEST_ID_HEADER.encode("latin-1"), b"")
        request_id = incoming.decode("latin-1").strip() or str(uuid.uuid4())
        scope.setdefault("state", {})
        scope["state"]["request_id"] = request_id

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = message.setdefault("headers", [])
                response_headers.append(
                    (X_REQUEST_ID_HEADER.encode("latin-1"), request_id.encode("latin-1"))
                )
            await send(message)

        await self.app(scope, receive, send_wrapper)
