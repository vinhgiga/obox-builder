"""CSRF tokens tied to the DB session, plus an Origin/Referer guard."""

from __future__ import annotations

import secrets
from urllib.parse import urlsplit

from fastapi import Request

from obox_builder.contrib.auth.models import Session as AuthSession
from obox_builder.core.exceptions import PermissionDenied

CSRF_FIELD_NAME = "csrf_token"
CSRF_HEADER_NAME = "x-csrf-token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def get_csrf_token(auth_session: AuthSession) -> str:
    """Return the session's CSRF token, creating it on first use."""
    token = auth_session.data.get(CSRF_FIELD_NAME)
    if not isinstance(token, str) or not token:
        token = secrets.token_urlsafe(32)
        auth_session.data = {**auth_session.data, CSRF_FIELD_NAME: token}
    return token


def check_origin(request: Request) -> bool:
    """Reject unsafe requests whose Origin/Referer belongs to another host."""
    if request.method in SAFE_METHODS:
        return True
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source:
        return True
    host = urlsplit(source).netloc
    return host == request.url.netloc


def validate_csrf(request: Request, auth_session: AuthSession, provided: str | None) -> None:
    """Validate Origin plus the session-bound token; raise 403 on failure."""
    if not check_origin(request):
        raise PermissionDenied("Cross-origin request rejected.")
    if provided is None:
        provided = request.headers.get(CSRF_HEADER_NAME)
    expected = auth_session.data.get(CSRF_FIELD_NAME)
    if not isinstance(expected, str) or not expected or not provided:
        raise PermissionDenied("CSRF check failed: token missing.")
    if not secrets.compare_digest(provided, expected):
        raise PermissionDenied("CSRF check failed: token mismatch.")
