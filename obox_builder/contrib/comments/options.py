"""Declarations for row comments (``Model.__comments__``)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from obox_builder.core.exceptions import ImproperlyConfigured

KNOWN_KEYS = frozenset({"max_length", "allow_replies", "edit_window_seconds"})
DB_MAX_LENGTH = 20_000


@dataclass(frozen=True)
class CommentOptions:
    """Validated options for one commentable model.

    ``None`` means "inherit the ``COMMENTS_*`` setting"; booleans default to
    the framework defaults rather than to ``False``.
    """

    enabled: bool = False
    max_length: int | None = None
    allow_replies: bool = True
    edit_window_seconds: int | None = None


DISABLED = CommentOptions()


def parse_comments(raw: Any) -> CommentOptions:
    """Parse a ``Model.__comments__`` declaration into ``CommentOptions``."""
    if raw is None or raw is False:
        return DISABLED
    if raw is True:
        return CommentOptions(enabled=True)
    if not isinstance(raw, Mapping):
        raise ImproperlyConfigured(
            f"__comments__ must be True, False or a mapping, not {type(raw).__name__}."
        )
    unknown = set(raw) - KNOWN_KEYS
    if unknown:
        names = ", ".join(sorted(str(key) for key in unknown))
        raise ImproperlyConfigured(f"__comments__ has unknown keys: {names}.")

    max_length = _optional_int(raw, "max_length", minimum=1, maximum=DB_MAX_LENGTH)
    edit_window = _optional_int(raw, "edit_window_seconds", minimum=0, maximum=None)
    allow_replies = raw.get("allow_replies", True)
    if not isinstance(allow_replies, bool):
        raise ImproperlyConfigured("__comments__['allow_replies'] must be a boolean.")
    return CommentOptions(
        enabled=True,
        max_length=max_length,
        allow_replies=allow_replies,
        edit_window_seconds=edit_window,
    )


def _optional_int(
    raw: Mapping[Any, Any],
    key: str,
    *,
    minimum: int,
    maximum: int | None,
) -> int | None:
    if key not in raw or raw[key] is None:
        return None
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ImproperlyConfigured(f"__comments__[{key!r}] must be an integer or None.")
    if value < minimum or (maximum is not None and value > maximum):
        bounds = f"{minimum}..{maximum}" if maximum is not None else f">= {minimum}"
        raise ImproperlyConfigured(f"__comments__[{key!r}] must be {bounds}, got {value}.")
    return value
