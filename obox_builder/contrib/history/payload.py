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
    fields: dict[str, list[Any]] = {}
    truncated: dict[str, dict[str, dict[str, Any]]] = {}
    for name in sorted(change.fields):
        old, new = change.fields[name]
        prepared: list[Any] = []
        for side, value in (("old", old), ("new", new)):
            overflow = _overflow(value)
            if overflow is None:
                prepared.append(value)
            else:
                prepared.append(_preview(value))
                truncated.setdefault(name, {})[side] = overflow
        fields[name] = prepared
    return {
        "fields": fields,
        "m2m": {},
        "redacted": sorted(change.redacted),
        "truncated": truncated,
        "unknown_old": sorted(change.unknown_old),
    }


def _overflow(value: Any) -> dict[str, Any] | None:
    limit = int(settings.HISTORY_MAX_VALUE_BYTES)
    raw = _compact_json(value)
    size = len(raw.encode("utf-8"))
    if size <= limit:
        return None
    return {"len": size, "sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest()}


def _preview(value: Any) -> str:
    return _compact_json(value)[:PREVIEW_CHARS]


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
