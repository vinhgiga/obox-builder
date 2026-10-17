"""Text-like field types: text, long_text, email, url, phone."""

from __future__ import annotations

import re
from typing import Annotated, Any

from pydantic import Field as PydField

from obox_builder.fields.base import Cell, FieldSpec, FieldType, Operator

EQ = Operator("eq", "is")
NE = Operator("ne", "is not")
GT = Operator("gt", ">")
GTE = Operator("gte", ">=")
LT = Operator("lt", "<")
LTE = Operator("lte", "<=")
CONTAINS = Operator("contains", "contains")
STARTSWITH = Operator("startswith", "starts with")
ENDSWITH = Operator("endswith", "ends with")
IN = Operator("in", "is any of")
ISNULL = Operator("isnull", "is empty")

TEXT_OPERATORS = {op.name: op for op in (EQ, NE, CONTAINS, STARTSWITH, ENDSWITH, IN, ISNULL)}
PROSE_OPERATORS = {op.name: op for op in (EQ, NE, CONTAINS, ISNULL)}

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_RE = re.compile(r"^[0-9+\-().\s]{6,25}$")


class Text(FieldType):
    name = "text"
    widget = "text"
    searchable = True
    operators = TEXT_OPERATORS

    def annotation(self, spec: FieldSpec) -> Any:
        if spec.required:
            return Annotated[str, PydField(min_length=1)]
        if spec.nullable:
            return str | None
        return str

    def from_input(self, raw: Any, spec: FieldSpec) -> Any:
        if raw is None:
            return None if spec.nullable else ""
        if not isinstance(raw, str):
            return raw
        value = raw.strip()
        if value == "" and spec.nullable and not spec.required:
            return None
        return value

    def to_input(self, value: Any, spec: FieldSpec) -> str:
        return "" if value is None else str(value)

    def cell(self, value: Any, spec: FieldSpec) -> Cell:
        return Cell(text="" if value is None else str(value), value=value)

    def diff_repr(self, value: Any) -> Any:
        return None if value is None else str(value)


class LongText(Text):
    name = "long_text"
    widget = "textarea"
    operators = PROSE_OPERATORS

    def cell(self, value: Any, spec: FieldSpec) -> Cell:
        if value is None:
            return Cell(text="", value=value)
        text = " ".join(str(value).split())
        if len(text) > 80:
            text = text[:77] + "..."
        return Cell(text=text, value=value)


class Email(Text):
    name = "email"
    widget = "email"

    def validate(self, value: Any, spec: FieldSpec) -> str | None:
        if value and not _EMAIL_RE.match(str(value)):
            return "Enter a valid email address."
        return None


class URL(Text):
    name = "url"
    widget = "url"

    def validate(self, value: Any, spec: FieldSpec) -> str | None:
        if value and not str(value).startswith(("http://", "https://")):
            return "Enter a valid URL (http:// or https://)."
        return None


class Phone(Text):
    name = "phone"
    widget = "text"

    def validate(self, value: Any, spec: FieldSpec) -> str | None:
        if value and not _PHONE_RE.match(str(value)):
            return "Enter a valid phone number."
        return None
