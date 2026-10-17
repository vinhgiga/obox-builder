"""Safe redirect handling for login ``next`` parameters."""

from __future__ import annotations

from urllib.parse import urlsplit

from obox_builder.conf import settings


def is_safe_url(url: str | None) -> bool:
    """Only same-host, relative paths are safe as redirect targets."""
    if not url:
        return False
    if not url.startswith("/"):
        return False
    if url.startswith("//"):  # protocol-relative URLs point at another host
        return False
    parts = urlsplit(url)
    return parts.scheme == "" and parts.netloc == ""


def safe_next_url(candidate: str | None) -> str:
    """Return ``candidate`` when it is a safe redirect target, else the login redirect."""
    if candidate and is_safe_url(candidate):
        return candidate
    return settings.LOGIN_REDIRECT_URL
