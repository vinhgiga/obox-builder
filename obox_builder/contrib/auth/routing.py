"""Preserve sliding-session cookies on responses returned directly by views."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Request, Response
from fastapi.routing import APIRoute

from obox_builder.conf import settings


class SessionRenewalRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def handle(request: Request) -> Response:
            response = await handler(request)
            renewal = getattr(request.state, "session_renewal_cookie", None)
            prefix = f"{settings.SESSION_COOKIE_NAME}="
            if renewal and response.status_code < 400 and not any(
                cookie.lstrip().startswith(prefix)
                for cookie in response.headers.getlist("set-cookie")
            ):
                response.headers.append("set-cookie", renewal)
            return response

        return handle
