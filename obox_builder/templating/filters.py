"""Built-in template filters."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from urllib.parse import urlencode


def format_datetime(value: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Format a datetime (aware or naive); empty string for ``None``."""
    if value is None:
        return ""
    return value.strftime(fmt)


def filesizeformat(value: int | float) -> str:
    """Human-readable file size (1.5 KB, 2.0 MB, ...)."""
    size = float(value)
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            if unit == "bytes":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


def querystring(values: Mapping[str, object], **updates: object) -> str:
    """Merge a mapping with overrides into an encoded query string (``a=1&b=2``)."""
    merged = {key: value for key, value in values.items() if value not in (None, "")}
    for key, value in updates.items():
        if value in (None, ""):
            merged.pop(key, None)
        else:
            merged[key] = value
    return urlencode(merged)
