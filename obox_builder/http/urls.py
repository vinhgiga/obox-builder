"""Safe redirect handling for login ``next`` parameters."""

from __future__ import annotations

from urllib.parse import urlsplit

from obox_builder.conf import settings

MAX_URL_LENGTH = 2048


def is_safe_url(url: str | None) -> bool:
    """Only same-host, relative paths are safe as redirect targets."""
    if not url or len(url) > MAX_URL_LENGTH:
        return False
    return _is_safe_relative_path(url) and _is_safe_relative_path(url.replace("\\", "/"))


def _is_safe_relative_path(url: str) -> bool:
    if not url.startswith("/") or url.startswith("//"):
        return False
    if any(ord(char) < 32 or ord(char) == 127 for char in url):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme == "" and parts.netloc == ""


def safe_next_url(candidate: str | None) -> str:
    """Return ``candidate`` when it is a safe redirect target, else the login redirect."""
    if candidate and is_safe_url(candidate):
        return candidate
    return settings.LOGIN_REDIRECT_URL
