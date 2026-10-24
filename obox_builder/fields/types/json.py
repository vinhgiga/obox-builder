"""JSON field type with bounded, schema-free editing."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from obox_builder.fields.base import Cell, FieldSpec, FieldType
from obox_builder.fields.types.text import ISNULL

JSON_OPERATORS = {op.name: op for op in (ISNULL,)}


@dataclass(frozen=True)
class _InvalidJson:
    raw: str


def compact_json(value: Any, limit: int = 80) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError, RecursionError):  # pragma: no cover - defensive
        text = str(value)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 3] + "..."
    return text


class Json(FieldType):
    name = "json"
    widget = "json"
    sortable = False
    operators = JSON_OPERATORS

    def annotation(self, spec: FieldSpec) -> Any:
        return Any

    def from_input(self, raw: Any, spec: FieldSpec) -> Any:
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return None
        if not isinstance(raw, str):
            return raw
        if len(raw.encode("utf-8")) > int(spec.options.get("max_bytes", 65_536)):
            return _InvalidJson(raw)
        try:
            return json.loads(raw)
        except (ValueError, RecursionError):
            return _InvalidJson(raw)

    def validate(self, value: Any, spec: FieldSpec) -> str | None:
        if value is None:
            return None
        if isinstance(value, _InvalidJson):
            return "Enter valid JSON."
        try:
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError, RecursionError):
            return "Enter valid JSON."
        limit = int(spec.options.get("max_bytes", 65_536))
        if len(encoded.encode("utf-8")) > limit:
            return f"JSON must be {limit} bytes or smaller."
        return None

    def to_input(self, value: Any, spec: FieldSpec) -> str:
        if value is None:
            return ""
        if isinstance(value, _InvalidJson):
            return value.raw
        try:
            return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
        except (TypeError, ValueError, RecursionError):
            return ""

    def cell(self, value: Any, spec: FieldSpec) -> Cell:
        if value is None:
            return Cell(text="", value=value)
        return Cell(text=compact_json(value), value=value, kind="json")

    def diff_repr(self, value: Any) -> Any:
        return value
