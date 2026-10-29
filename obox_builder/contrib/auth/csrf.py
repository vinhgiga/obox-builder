"""CSRF tokens tied to the DB session, plus an Origin/Referer guard."""

from __future__ import annotations

import secrets
from urllib.parse import urlsplit

from fastapi import Request

from obox_builder.contrib.auth.models import Session as AuthSession
from obox_builder.exceptions import PermissionDenied

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
    """Check unsafe request origins and require a secure Referer without Origin."""
    if request.method in SAFE_METHODS:
        return True
    origin = request.headers.get("origin")
    if origin is not None:
        return _is_same_origin(origin, str(request.url), require_bare_origin=True)
    if request.url.scheme.lower() != "https":
        return True
    referer = request.headers.get("referer")
    if referer is None:
        return False
    return _is_same_origin(referer, str(request.url))


def _is_same_origin(source: str, target: str, *, require_bare_origin: bool = False) -> bool:
    source_origin = _origin_tuple(source, require_bare_origin=require_bare_origin)
    return source_origin is not None and source_origin == _origin_tuple(target)


def _origin_tuple(url: str, *, require_bare_origin: bool = False) -> tuple[str, str, int] | None:
    if any(ord(char) < 32 or ord(char) == 127 for char in url):
        return None
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    hostname = parsed.hostname
    if (
        scheme not in {"http", "https"}
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or (require_bare_origin and (parsed.path or parsed.query or parsed.fragment))
    ):
        return None
    if port is None:
        port = 443 if scheme == "https" else 80
    return scheme, hostname.lower(), port


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
