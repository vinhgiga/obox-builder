"""Django-shaped CSRF tokens for native request/response objects."""

from __future__ import annotations

import secrets
import string
from hmac import compare_digest
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import Response

from obox.conf import settings
from obox.core.exceptions import PermissionDenied

_CHARS = string.ascii_letters + string.digits


def _secret(value: str) -> str | None:
    if len(value) not in (32, 64) or any(char not in _CHARS for char in value):
        return None
    if len(value) == 32:
        return value
    mask, cipher = value[:32], value[32:]
    return "".join(
        _CHARS[(_CHARS.index(right) - _CHARS.index(left)) % len(_CHARS)]
        for left, right in zip(mask, cipher, strict=True)
    )


def get_token(request: Request) -> str:
    secret = request.scope.get("_csrf_secret") or _secret(
        request.cookies.get(settings.CSRF_COOKIE_NAME, "")
    )
    if secret is None:
        secret = "".join(secrets.choice(_CHARS) for _ in range(32))
    request.scope["_csrf_secret"] = secret
    mask = "".join(secrets.choice(_CHARS) for _ in range(32))
    cipher = "".join(
        _CHARS[(_CHARS.index(left) + _CHARS.index(right)) % len(_CHARS)]
        for left, right in zip(mask, secret, strict=True)
    )
    return mask + cipher


async def _check(request: Request) -> None:
    if request.method in ("GET", "HEAD", "OPTIONS", "TRACE"):
        return
    origin = request.headers.get("origin")
    if (
        origin is not None
        and origin != f"{request.url.scheme}://{request.url.netloc}"
        and origin not in settings.CSRF_TRUSTED_ORIGINS
    ):
        raise PermissionDenied("Origin checking failed.")
    if origin is None and request.url.scheme == "https":
        referer = urlsplit(request.headers.get("referer", ""))
        if referer.scheme != "https" or referer.netloc != request.url.netloc:
            raise PermissionDenied("Referer checking failed.")
    expected = _secret(request.cookies.get(settings.CSRF_COOKIE_NAME, ""))
    provided = request.headers.get("x-csrftoken", "")
    if request.method == "POST" and request.headers.get("content-type", "").startswith(
        "application/x-www-form-urlencoded"
    ):
        from obox._http import read_form

        data = await read_form(request)
        provided = str(data.get("csrfmiddlewaretoken", "")) or provided
    actual = _secret(provided)
    if expected is None or actual is None or not compare_digest(expected, actual):
        raise PermissionDenied("CSRF verification failed.")


def _set_cookie(request: Request, response: Response) -> None:
    secret = request.scope.get("_csrf_secret")
    if secret:
        response.set_cookie(
            settings.CSRF_COOKIE_NAME,
            secret,
            max_age=settings.CSRF_COOKIE_AGE,
            path=settings.CSRF_COOKIE_PATH,
            domain=settings.CSRF_COOKIE_DOMAIN,
            httponly=settings.CSRF_COOKIE_HTTPONLY,
            samesite=settings.CSRF_COOKIE_SAMESITE,
            secure=settings.CSRF_COOKIE_SECURE,
        )
        vary = response.headers.get("Vary", "")
        response.headers["Vary"] = ", ".join(
            dict.fromkeys([part.strip() for part in vary.split(",") if part.strip()] + ["Cookie"])
        )
