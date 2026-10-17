"""Choice field type: single_select (Enum or explicit choices)."""

from __future__ import annotations

from typing import Any

from obox_builder.fields.base import Cell, FieldSpec, FieldType
from obox_builder.fields.types.text import EQ, IN, ISNULL, NE

CHOICE_OPERATORS = {op.name: op for op in (EQ, NE, IN, ISNULL)}


def normalize_choices(raw: Any) -> list[tuple[str, str]]:
    """Accept ``['draft', 'paid']`` or ``[('draft', 'Draft'), ...]``."""
    choices: list[tuple[str, str]] = []
    for item in raw or []:
        if isinstance(item, (tuple, list)) and len(item) == 2:
            value, label = item
            choices.append((str(value), str(label)))
        else:
            value = str(getattr(item, "value", item))
            label = str(getattr(item, "name", item)).replace("_", " ").title()
            choices.append((value, label))
    return choices


class SingleSelect(FieldType):
    name = "single_select"
    widget = "select"
    operators = CHOICE_OPERATORS

    def choices(self, spec: FieldSpec) -> list[tuple[str, str]]:
        return normalize_choices(spec.options.get("choices"))

    def annotation(self, spec: FieldSpec) -> Any:
        if not spec.required:
            return str | None
        return str

    def from_input(self, raw: Any, spec: FieldSpec) -> Any:
        if raw is None:
            return None
        value = str(raw).strip()
        if value == "" and spec.nullable and not spec.required:
            return None
        return value

    def validate(self, value: Any, spec: FieldSpec) -> str | None:
        if value in (None, ""):
            return None
        values = {choice for choice, _label in self.choices(spec)}
        if value not in values:
            return "Select a valid choice."
        return None

    def cell(self, value: Any, spec: FieldSpec) -> Cell:
        if value is None:
            return Cell(text="", value=value)
        labels = dict(self.choices(spec))
        return Cell(text=labels.get(str(value), str(value)), value=value, kind="badge")

    def diff_repr(self, value: Any) -> Any:
        return None if value is None else str(value)
