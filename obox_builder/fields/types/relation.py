"""Relation field types: link (single FK). multi_link arrives with M11."""

from __future__ import annotations

from typing import Any

from obox_builder.fields.base import Cell, FieldSpec, FieldType
from obox_builder.fields.types.text import EQ, IN, ISNULL, NE

LINK_OPERATORS = {op.name: op for op in (EQ, NE, IN, ISNULL)}


class Link(FieldType):
    name = "link"
    widget = "select"
    operators = LINK_OPERATORS

    def annotation(self, spec: FieldSpec) -> Any:
        if not spec.required:
            return int | None
        return int

    def from_input(self, raw: Any, spec: FieldSpec) -> Any:
        if raw is None:
            return None
        if isinstance(raw, str):
            value = raw.strip()
            if value == "":
                return None
            return value
        return raw

    def diff_repr(self, value: Any) -> Any:
        if value is None or isinstance(value, (int, str)):
            return value
        return str(value)

    def cell(self, value: Any, spec: FieldSpec) -> Cell:
        if value is None:
            return Cell(text="", value=None, kind="link")
        if isinstance(value, (int, str)):
            return Cell(text=str(value), value=value, kind="link")
        return Cell(text=str(value), value=value, kind="link")
