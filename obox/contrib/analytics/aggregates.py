"""Bounded scalar aggregation over an authorized resource query."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from enum import Enum
from typing import Any, Literal

from pydantic_core import to_jsonable_python

from obox.core.exceptions import BuilderValidationError, ImproperlyConfigured

Operation = Literal["count", "count_numbers", "sum", "avg", "min", "max"]


@dataclass(frozen=True)
class Measure:
    operation: Operation = "count"
    field: str | None = None
    label: str | None = None

    def __post_init__(self) -> None:
        if (
            self.operation not in {"count", "sum", "avg", "min", "max"}
            or (self.field is not None if self.operation == "count" else not self.field)
            or (self.field is not None and not isinstance(self.field, str))
            or (self.label is not None and (not isinstance(self.label, str) or not self.label))
        ):
            raise ImproperlyConfigured("Invalid measure.")


@dataclass(frozen=True)
class FieldCount(Measure):
    def __post_init__(self) -> None:
        if (
            self.operation not in {"count", "count_numbers"}
            or not isinstance(self.field, str)
            or not self.field
            or (self.label is not None and (not isinstance(self.label, str) or not self.label))
        ):
            raise ImproperlyConfigured("Invalid pivot field count.")


def encode(value: Any) -> Any:
    if isinstance(value, datetime):
        value = value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC)
    if isinstance(value, Enum):
        return encode(value.value)
    if isinstance(value, int) and abs(value) > 2**53 - 1:
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        raise BuilderValidationError({"measure": ["Non-finite values cannot be displayed."]})
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise BuilderValidationError({"measure": ["Non-finite values cannot be displayed."]})
        return str(value)
    return to_jsonable_python(value)


@dataclass
class State:
    records: int = 0
    present: int = 0
    value: Any = None

    def merge(self, other: State, operation: Operation) -> None:
        self.records += other.records
        self.present += other.present
        if other.value is None:
            return
        if self.value is None:
            self.value = other.value
        elif operation == "min":
            self.value = min(self.value, other.value)
        elif operation == "max":
            self.value = max(self.value, other.value)
        else:
            self.value += other.value

    def result(self, operation: Operation) -> Any:
        if operation == "count":
            return self.records
        if operation == "avg" and self.present:
            with localcontext() as context:
                context.prec = 38
                value = Decimal(self.value) if isinstance(self.value, int) else self.value
                return value / self.present
        return self.value


def exceeded() -> BuilderValidationError:
    return BuilderValidationError(
        {"filter": ["Too many groups. Narrow the filters and try again."]}, code="aggregate_limit"
    )
