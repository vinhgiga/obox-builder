"""Request-local locale state shared by the framework's layers."""

from __future__ import annotations

import re
from contextvars import ContextVar, Token

_LANGUAGE_TAG = re.compile(r"^[a-z]{1,8}(?:-[a-z0-9]{1,8})*$")
_active_locale: ContextVar[str | None] = ContextVar("obox_active_locale", default=None)


def normalize_language_tag(value: str) -> str:
    """Normalize a standard language tag and reject malformed or oversized input."""
    normalized = value.strip().replace("_", "-").lower()
    if len(normalized) > 500 or not _LANGUAGE_TAG.fullmatch(normalized):
        raise ValueError(f"Invalid language code: {value!r}")
    return normalized


def get_active_locale(default: str | None = None) -> str | None:
    """Return the active request locale, or ``default`` outside a localized request."""
    return _active_locale.get() or default


def activate_locale(locale: str) -> Token[str | None]:
    """Set the locale for the current async context and return its reset token."""
    return _active_locale.set(locale)


def reset_locale(token: Token[str | None]) -> None:
    """Restore the locale that was active before ``activate_locale``."""
    _active_locale.reset(token)
