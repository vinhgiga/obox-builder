from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ensure_aware(value: datetime) -> datetime:
    """SQLite returns naive datetimes; treat stored values as timezone.utc."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value
