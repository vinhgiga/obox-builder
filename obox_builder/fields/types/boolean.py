"""Boolean field type."""

from __future__ import annotations

from typing import Any

from obox_builder.fields.base import Cell, FieldSpec, FieldType
from obox_builder.fields.types.text import EQ, ISNULL, NE

BOOLEAN_OPERATORS = {op.name: op for op in (EQ, NE, ISNULL)}

_TRUE = {"on", "true", "1", "yes", "y"}


class Checkbox(FieldType):
    name = "checkbox"
    widget = "checkbox"
    operators = BOOLEAN_OPERATORS

    def annotation(self, spec: FieldSpec) -> Any:
        if not spec.required:
            return bool | None
        return bool

    def from_input(self, raw: Any, spec: FieldSpec) -> Any:
        if isinstance(raw, bool):
            return raw
        if raw is None or raw == "":
            return None if spec.nullable and not spec.required else False
        return str(raw).strip().lower() in _TRUE

    def from_api(self, raw: Any, spec: FieldSpec) -> Any:
        return raw

    def to_input(self, value: Any, spec: FieldSpec) -> str:
        return "true" if value else ""

    def cell(self, value: Any, spec: FieldSpec) -> Cell:
        return Cell(text="Yes" if value else "No", value=value, kind="boolean")

    def diff_repr(self, value: Any) -> Any:
        return bool(value) if value is not None else None
