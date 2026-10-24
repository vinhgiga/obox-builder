"""Scalar and collection relation fields."""

from __future__ import annotations

from types import GenericAlias
from typing import Any

import sqlalchemy as sa

from obox_builder.fields.base import Cell, FieldSpec, FieldType
from obox_builder.fields.types.text import EQ, IN, ISNULL, NE

LINK_OPERATORS = {op.name: op for op in (EQ, NE, IN, ISNULL)}


class Link(FieldType):
    name = "link"
    widget = "select"
    operators = LINK_OPERATORS

    def annotation(self, spec: FieldSpec) -> Any:
        value_type = spec.column.type.python_type if spec.column is not None else int
        return value_type if spec.required else value_type | None

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


class MultiLink(FieldType):
    name = "multi_link"
    widget = "multiselect"
    sortable = False

    def annotation(self, spec: FieldSpec) -> Any:
        target = spec.options["target"]
        return GenericAlias(list, sa.inspect(target).primary_key[0].type.python_type)

    def from_input(self, raw: Any, spec: FieldSpec) -> list[Any]:
        if raw is None or raw == "":
            return []
        values = list(raw) if isinstance(raw, (list, tuple)) else [raw]
        return [value for value in values if value != ""]

    def cell(self, value: Any, spec: FieldSpec) -> Cell:
        values = list(value or [])
        return Cell(text=", ".join(str(item) for item in values), value=values)
