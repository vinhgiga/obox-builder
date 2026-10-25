"""Actor/source context for history entries.

Resolution order at flush time: a late-bound resolver stored in
``session.info["obox.history.ctx"]`` (web requests), then the ContextVar set by
``use(...)`` (CLI/jobs), then the ``system`` context.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from obox_builder.conf import settings

logger = logging.getLogger("obox.history")

CTX_KEY = "obox.history.ctx"
_INVALID_REQUEST_ID = re.compile(r"[^A-Za-z0-9._-]+")
MAX_REQUEST_ID = 64

_CONTEXT: ContextVar[HistoryContext | None] = ContextVar("obox_history_context", default=None)


@dataclass(frozen=True, slots=True)
class HistoryContext:
    """Who changed what, from where."""

    actor_id: int | None = None
    actor_label: str = "system"
    source: str = "system"
    request_id: str | None = None
    ip: str | None = None
    user_agent: str | None = None
    reason: str | None = None
    reverts_id: int | None = None

    @classmethod
    def system(cls) -> HistoryContext:
        return cls()


def sanitize_request_id(value: Any) -> str | None:
    """Normalise a client-supplied request id to ``[A-Za-z0-9._-]{1,64}``.

    The header is attacker-controlled, so it is treated as a correlation hint,
    never as evidence.
    """
    if not isinstance(value, str):
        return None
    cleaned = _INVALID_REQUEST_ID.sub("", value.strip())[:MAX_REQUEST_ID]
    return cleaned or None


@asynccontextmanager
async def use(
    *,
    source: str = "cli",
    actor_label: str = "cli",
    actor_id: int | None = None,
    request_id: str | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    reason: str | None = None,
    reverts_id: int | None = None,
) -> AsyncIterator[HistoryContext]:
    """Run a block under an explicit history context (CLI, background jobs)."""
    context = HistoryContext(
        actor_id=actor_id,
        actor_label=actor_label,
        source=source,
        request_id=sanitize_request_id(request_id),
        ip=ip,
        user_agent=user_agent,
        reason=reason,
        reverts_id=reverts_id,
    )
    token = _CONTEXT.set(context)
    try:
        yield context
    finally:
        _CONTEXT.reset(token)


def bind(db: Any, request: Any, *, source: str = "admin_ui") -> None:
    """Attach a late-bound context resolver to a session.

    Called from router dependencies that run *before* authentication: the
    resolver reads ``request.state`` at flush time, when the user is known.
    """

    def resolve() -> HistoryContext:
        return from_request(request, source=source)

    db.info[CTX_KEY] = resolve


def from_request(request: Any, *, source: str = "admin_ui") -> HistoryContext:
    """Build a context from the request state (user, session, request id)."""
    user = getattr(request.state, "user", None)
    actor_id: int | None = None
    actor_label = "anonymous"
    if user is not None and getattr(user, "id", None) is not None:
        actor_id = int(user.id)
        actor_label = str(getattr(user, "username", "") or user)
    auth_session = getattr(request.state, "auth_session", None)
    return HistoryContext(
        actor_id=actor_id,
        actor_label=actor_label,
        source=source,
        request_id=sanitize_request_id(getattr(request.state, "request_id", None)),
        ip=_client_ip(request),
        user_agent=_truncate(getattr(auth_session, "user_agent", None), 255),
    )


def resolve_context(session: Any) -> HistoryContext:
    """Resolve the active context from the session, ContextVar or fallback."""
    stored: Any = session.info.get(CTX_KEY)
    if callable(stored):
        try:
            resolved = stored()
        except Exception:  # noqa: BLE001 - context must never break a flush
            logger.warning("History context resolver failed; using 'system'.", exc_info=True)
            resolved = None
        if isinstance(resolved, HistoryContext):
            return resolved
    elif isinstance(stored, HistoryContext):
        return stored
    current = _CONTEXT.get()
    if current is not None:
        return current
    return HistoryContext.system()


def _client_ip(request: Any) -> str | None:
    peer = request.client.host if getattr(request, "client", None) else None
    trusted = settings.HISTORY_TRUSTED_PROXIES
    if trusted and peer in trusted:
        header = request.headers.get("x-forwarded-for", "")
        first = header.split(",")[0].strip()
        if first:
            return _truncate(first, 45)
    return _truncate(peer, 45)


def _truncate(value: Any, length: int) -> str | None:
    if value is None:
        return None
    text = str(value)[:length]
    return text or None


def _reset() -> None:
    """Test hook: clear the ambient context."""
    _CONTEXT.set(None)
