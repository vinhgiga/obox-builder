"""Declarations for row-history tracking (``Model.__history__``)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from obox_builder.exceptions import ImproperlyConfigured

KNOWN_KEYS = frozenset({"fields", "exclude", "relations", "mask", "retention_days"})


@dataclass(frozen=True)
class HistoryOptions:
    """Validated options for one tracked model."""

    enabled: bool = False
    fields: tuple[str, ...] | None = None
    exclude: frozenset[str] = frozenset()
    relations: tuple[str, ...] = ()
    mask: frozenset[str] = frozenset()
    retention_days: int | None = None


DISABLED = HistoryOptions()


def parse_history(raw: Any) -> HistoryOptions:
    """Parse a ``Model.__history__`` declaration into ``HistoryOptions``."""
    if raw is None or raw is False:
        return DISABLED
    if raw is True:
        return HistoryOptions(enabled=True)
    if not isinstance(raw, Mapping):
        raise ImproperlyConfigured(
            f"__history__ must be True, False or a mapping, not {type(raw).__name__}."
        )
    unknown = set(raw) - KNOWN_KEYS
    if unknown:
        names = ", ".join(sorted(str(key) for key in unknown))
        raise ImproperlyConfigured(f"__history__ has unknown keys: {names}.")
    raw_fields = raw.get("fields")
    return HistoryOptions(
        enabled=True,
        fields=tuple(str(name) for name in raw_fields) if raw_fields is not None else None,
        exclude=frozenset(str(name) for name in raw.get("exclude", ())),
        relations=tuple(str(name) for name in raw.get("relations", ())),
        mask=frozenset(str(name) for name in raw.get("mask", ())),
        retention_days=raw.get("retention_days"),
    )
