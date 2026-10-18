"""Numeric field types: integer and decimal."""

from __future__ import annotations

from decimal import Decimal as DecimalValue
from typing import Any

from obox_builder.fields.base import Cell, FieldSpec, FieldType
from obox_builder.fields.types.text import EQ, GT, GTE, IN, ISNULL, LT, LTE, NE

NUMBER_OPERATORS = {op.name: op for op in (EQ, NE, GT, GTE, LT, LTE, IN, ISNULL)}


def _empty(raw: Any) -> bool:
    return raw is None or (isinstance(raw, str) and raw.strip() == "")


class Integer(FieldType):
    name = "integer"
    widget = "number"
    operators = NUMBER_OPERATORS

    def annotation(self, spec: FieldSpec) -> Any:
        if not spec.required:
            return int | None
        return int

    def from_input(self, raw: Any, spec: FieldSpec) -> Any:
        if _empty(raw):
            return None
        if isinstance(raw, str):
            return raw.strip()
        return raw

    def diff_repr(self, value: Any) -> Any:
        return None if value is None else int(value)

    def cell(self, value: Any, spec: FieldSpec) -> Cell:
        return Cell(text="" if value is None else str(value), value=value, kind="number")


class Decimal(FieldType):
    name = "decimal"
    widget = "number"
    operators = NUMBER_OPERATORS

    def annotation(self, spec: FieldSpec) -> Any:
        if not spec.required:
            return DecimalValue | None
        return DecimalValue

    def from_input(self, raw: Any, spec: FieldSpec) -> Any:
        if _empty(raw):
            return None
        if isinstance(raw, str):
            return raw.strip()
        return raw

    def to_input(self, value: Any, spec: FieldSpec) -> str:
        if value is None:
            return ""
        scale = spec.options.get("scale")
        if scale is not None and isinstance(value, DecimalValue):
            return f"{value:.{int(scale)}f}"
        return str(value)

    def diff_repr(self, value: Any) -> Any:
        return None if value is None else str(value)

    def cell(self, value: Any, spec: FieldSpec) -> Cell:
        if value is None:
            return Cell(text="", value=value, kind="number")
        return Cell(text=self.to_input(value, spec), value=value, kind="number")
