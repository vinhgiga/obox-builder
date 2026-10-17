"""Flash messages stored inside the auth session (``auth_session.data``).

Messages survive one redirect: they are written to the session row, committed
with the request transaction, and cleared the next time they are read.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request

MESSAGE_LEVELS = ("debug", "info", "success", "warning", "error")

_MESSAGES_KEY = "messages"


def _get_session(request: Request) -> Any | None:
    return getattr(request.state, "auth_session", None)


def add_session_message(session: Any, message: str, level: str = "info") -> None:
    """Store a flash message on an explicit session object.

    Use this when the message must survive a session swap (e.g. after a
    password change that deletes the current session and creates a new one).
    """
    if level not in MESSAGE_LEVELS:
        raise ValueError(f"Unknown message level {level!r}; expected one of {MESSAGE_LEVELS}.")
    messages = list(session.data.get(_MESSAGES_KEY, []))
    messages.append({"level": level, "message": str(message)})
    session.data = {**session.data, _MESSAGES_KEY: messages}


async def add_message(request: Request, message: str, level: str = "info") -> None:
    """Store a flash message on the session; a no-op without a session."""
    session = _get_session(request)
    if session is not None:
        add_session_message(session, message, level)


async def get_and_clear_messages(request: Request) -> list[dict[str, str]]:
    """Return pending flash messages and remove them from the session."""
    session = _get_session(request)
    if session is None:
        return []
    messages = list(session.data.get(_MESSAGES_KEY, []))
    if messages:
        session.data = {**session.data, _MESSAGES_KEY: []}
    return messages


async def clear_messages(request: Request) -> None:
    """Drop pending flash messages without reading them."""
    session = _get_session(request)
    if session is not None and session.data.get(_MESSAGES_KEY):
        session.data = {**session.data, _MESSAGES_KEY: []}
