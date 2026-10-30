"""Expiring CSRF tokens signed and bound to the resolved actor and request cookies."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Any
from urllib.parse import urlsplit

from starlette.requests import Request

from obox_builder.errors import PermissionDenied


class CSRF:
    def __init__(self, secret: str | bytes | None) -> None:
        self.secret = (
            secret.encode() if isinstance(secret, str) else secret or secrets.token_bytes(32)
        )
        if len(self.secret) < 32:
            raise ValueError("csrf_secret must contain at least 32 bytes.")

    def _sign(self, value: str, request: Request, principal: Any) -> str:
        binding = repr((getattr(principal, "id", None), sorted(request.cookies.items())))
        return hmac.new(self.secret, (value + binding).encode(), hashlib.sha256).hexdigest()

    def token(self, request: Request, principal: Any) -> str:
        value = f"{int(time.time())}.{secrets.token_urlsafe(24)}"
        return value + "." + self._sign(value, request, principal)

    def validate(self, request: Request, principal: Any, token: str | None = None) -> None:
        if not request.cookies:
            return
        source = request.headers.get("origin")
        if source is None and request.url.scheme == "https":
            source = request.headers.get("referer")
            if source is None:
                raise PermissionDenied("A same-origin Referer or Origin is required.")
        if source is not None:
            try:
                parsed = urlsplit(source)
                target = urlsplit(str(request.url))
                same = (
                    parsed.scheme in {"http", "https"}
                    and parsed.username is None
                    and parsed.password is None
                    and not any(ord(char) < 32 or ord(char) == 127 for char in source)
                    and (
                        "origin" not in request.headers
                        or not (parsed.path or parsed.query or parsed.fragment)
                    )
                    and parsed.hostname == target.hostname
                    and parsed.scheme == target.scheme
                    and (parsed.port or (443 if parsed.scheme == "https" else 80))
                    == (target.port or (443 if target.scheme == "https" else 80))
                )
            except ValueError:
                same = False
            if not same:
                raise PermissionDenied("Cross-origin request rejected.")
        provided = token or request.headers.get("x-csrf-token", "")
        try:
            stamp, nonce, signature = provided.split(".")
            age = time.time() - int(stamp)
        except (ValueError, TypeError) as exc:
            raise PermissionDenied("Invalid CSRF token.") from exc
        if not 0 <= age <= 3600 or not hmac.compare_digest(
            signature.encode(), self._sign(stamp + "." + nonce, request, principal).encode()
        ):
            raise PermissionDenied("Invalid or expired CSRF token.")
