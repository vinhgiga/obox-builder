"""HTTP client helpers for tests of obox-builder projects."""

from __future__ import annotations

from typing import Any

import httpx

DEFAULT_BASE_URL = "http://testserver"


def build_client(application: Any = None, *, base_url: str = DEFAULT_BASE_URL) -> httpx.AsyncClient:
    """Return an ``httpx.AsyncClient`` talking to the ASGI app over the transport.

    The default application is ``get_asgi_application()``, i.e. the one built
    from the project's ``ASGI_APPLICATION``/settings. Callers own the client
    lifecycle (``async with``).
    """
    if application is None:
        from obox_builder.core.application import get_asgi_application

        application = get_asgi_application()
    transport = httpx.ASGITransport(app=application)
    return httpx.AsyncClient(transport=transport, base_url=base_url)


def session_cookie(client: httpx.AsyncClient, token: str) -> None:
    """Attach a session token to the client the same way browsers do."""
    from obox_builder.conf import settings

    client.cookies.set(settings.SESSION_COOKIE_NAME, token)
