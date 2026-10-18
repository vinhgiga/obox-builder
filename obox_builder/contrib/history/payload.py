"""Build the versioned ``changes`` payload (schema v1).

Deterministic shape (keys sorted, all top-level keys always present) so a
golden test can freeze it. Compatibility rules: adding keys is backwards
compatible; changing the meaning of a key bumps ``schema_version``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from obox_builder.conf import settings
from obox_builder.contrib.history.tracker import PendingChange

SCHEMA_VERSION = 1
PREVIEW_CHARS = 256


def build_changes(change: PendingChange) -> dict[str, Any]:
    """The ``changes`` JSONB document for one captured row."""
    limit = int(settings.HISTORY_MAX_VALUE_BYTES)
    fields: dict[str, list[Any]] = {}
    truncated: dict[str, dict[str, dict[str, Any]]] = {}
    for name in sorted(change.fields):
        old, new = change.fields[name]
        prepared: list[Any] = []
        for side, value in (("old", old), ("new", new)):
            raw = _compact_json(value)
            data = raw.encode("utf-8")
            if len(data) <= limit:
                prepared.append(value)
            else:
                prepared.append(raw[:PREVIEW_CHARS])
                truncated.setdefault(name, {})[side] = {
                    "len": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
        fields[name] = prepared
    return {
        "fields": fields,
        "m2m": _m2m(change),
        "redacted": sorted(change.redacted),
        "masked": _masked(change),
        "truncated": truncated,
        "unknown_old": sorted(change.unknown_old),
    }


def _masked(change: PendingChange) -> dict[str, list[Any]]:
    """Partially masked values (``a***@x.com``); never revertable."""
    return {name: list(change.masked[name]) for name in sorted(change.masked)}


def _m2m(change: PendingChange) -> dict[str, dict[str, list[str]]]:
    """Sorted relation add/remove id lists; every side key is always present."""
    return {
        name: {"add": list(sides["add"]), "remove": list(sides["remove"])}
        for name, sides in sorted(change.m2m.items())
    }


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
