"""Temporal field types: date, datetime, time."""

from __future__ import annotations

import datetime as dt
from typing import Any

from obox_builder.fields.base import Cell, FieldSpec, FieldType
from obox_builder.fields.types.text import EQ, GT, GTE, IN, ISNULL, LT, LTE, NE


def _locale_format(value: dt.date | dt.datetime, pattern: str) -> str:
    from obox_builder.conf import settings

    if settings.LANGUAGE_CODE == "vi":
        pattern = pattern.replace("%Y-%m-%d", "%d/%m/%Y")
    return value.strftime(pattern)


TEMPORAL_OPERATORS = {op.name: op for op in (EQ, NE, GT, GTE, LT, LTE, IN, ISNULL)}


def _empty(raw: Any) -> bool:
    return raw is None or (isinstance(raw, str) and raw.strip() == "")


def _as_str(value: Any) -> Any:
    return None if value is None else str(value)


class Date(FieldType):
    name = "date"
    widget = "date"
    operators = TEMPORAL_OPERATORS

    def annotation(self, spec: FieldSpec) -> Any:
        if not spec.required:
            return dt.date | None
        return dt.date

    def from_input(self, raw: Any, spec: FieldSpec) -> Any:
        if _empty(raw):
            return None
        return raw.strip() if isinstance(raw, str) else raw

    def to_input(self, value: Any, spec: FieldSpec) -> str:
        if isinstance(value, str):
            return value
        return value.isoformat() if isinstance(value, dt.date) else ""

    def diff_repr(self, value: Any) -> Any:
        return value.isoformat() if isinstance(value, dt.date) else _as_str(value)

    def cell(self, value: Any, spec: FieldSpec) -> Cell:
        text = _locale_format(value, "%Y-%m-%d") if isinstance(value, dt.date) else ""
        return Cell(text=text, value=value, kind="date")


class DateTime(FieldType):
    name = "datetime"
    widget = "datetime"
    operators = TEMPORAL_OPERATORS

    def annotation(self, spec: FieldSpec) -> Any:
        if not spec.required:
            return dt.datetime | None
        return dt.datetime

    def from_input(self, raw: Any, spec: FieldSpec) -> Any:
        if _empty(raw):
            return None
        return raw.strip() if isinstance(raw, str) else raw

    def to_input(self, value: Any, spec: FieldSpec) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, dt.datetime):
            return value.strftime("%Y-%m-%dT%H:%M")
        return ""

    def diff_repr(self, value: Any) -> Any:
        return value.isoformat() if isinstance(value, dt.datetime) else _as_str(value)

    def cell(self, value: Any, spec: FieldSpec) -> Cell:
        text = _locale_format(value, "%Y-%m-%d %H:%M") if isinstance(value, dt.datetime) else ""
        return Cell(text=text, value=value, kind="datetime")


class Time(FieldType):
    name = "time"
    widget = "time"
    operators = TEMPORAL_OPERATORS

    def annotation(self, spec: FieldSpec) -> Any:
        if not spec.required:
            return dt.time | None
        return dt.time

    def from_input(self, raw: Any, spec: FieldSpec) -> Any:
        if _empty(raw):
            return None
        return raw.strip() if isinstance(raw, str) else raw

    def to_input(self, value: Any, spec: FieldSpec) -> str:
        if isinstance(value, str):
            return value
        return value.strftime("%H:%M") if isinstance(value, dt.time) else ""

    def diff_repr(self, value: Any) -> Any:
        return value.isoformat() if isinstance(value, dt.time) else _as_str(value)

    def cell(self, value: Any, spec: FieldSpec) -> Cell:
        text = value.strftime("%H:%M") if isinstance(value, dt.time) else ""
        return Cell(text=text, value=value, kind="time")
