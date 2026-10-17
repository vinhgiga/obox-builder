from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    return datetime.now(UTC)


def ensure_aware(value: datetime) -> datetime:
    """SQLite returns naive datetimes; treat stored values as UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value
