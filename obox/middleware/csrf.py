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
from obox.web.http import form_media_type, read_form

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


async def check_csrf(request: Request, *, multipart: bool = False) -> None:
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
    if expected is None:
        raise PermissionDenied("CSRF verification failed.")
    provided = request.headers.get("x-csrftoken", "")
    if request.method == "POST":
        media_type = form_media_type(request)
        if media_type == "application/x-www-form-urlencoded":
            data = await read_form(request)
        elif multipart and media_type == "multipart/form-data":
            data = await request.form()
        else:
            data = None
        token = data.get("csrfmiddlewaretoken") if data is not None else None
        if isinstance(token, str) and token:
            provided = token
    actual = _secret(provided)
    if actual is None or not compare_digest(expected, actual):
        raise PermissionDenied("CSRF verification failed.")


def set_csrf_cookie(request: Request, response: Response) -> None:
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
