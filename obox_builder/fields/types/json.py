"""JSON field type: read-only in forms until a structured editor arrives."""

from __future__ import annotations

import json
from typing import Any

from obox_builder.fields.base import Cell, FieldSpec, FieldType
from obox_builder.fields.types.text import ISNULL

JSON_OPERATORS = {op.name: op for op in (ISNULL,)}


def compact_json(value: Any, limit: int = 80) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):  # pragma: no cover - defensive
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
        return dict | None if spec.nullable else dict

    def cell(self, value: Any, spec: FieldSpec) -> Cell:
        if value is None:
            return Cell(text="", value=value)
        return Cell(text=compact_json(value), value=value, kind="json")

    def diff_repr(self, value: Any) -> Any:
        return value
