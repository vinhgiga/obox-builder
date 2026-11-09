"""Bounded scalar aggregation over an authorized resource query."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from enum import Enum
from typing import Any, Literal

import sqlalchemy as sa
from pydantic_core import to_jsonable_python

from obox_builder.errors import BuilderValidationError, ImproperlyConfigured

Operation = Literal["count", "sum", "avg", "min", "max"]


@dataclass(frozen=True)
class Measure:
    operation: Operation = "count"
    field: str | None = None

    def __post_init__(self) -> None:
        valid_field = (
            self.field is None
            if self.operation == "count"
            else (isinstance(self.field, str) and bool(self.field))
        )
        if self.operation not in {"count", "sum", "avg", "min", "max"} or not valid_field:
            raise ImproperlyConfigured("Use count without a field, or a numeric field measure.")


@dataclass(frozen=True)
class Aggregation:
    rows: str
    measure: Measure = field(default_factory=Measure)
    columns: str | None = None
    row_sort: Literal["group", "value"] = "group"
    column_sort: Literal["group", "value"] = "group"
    row_descending: bool = False
    column_descending: bool = False

    @property
    def fields(self) -> tuple[str, ...]:
        return tuple(name for name in (self.rows, self.columns, self.measure.field) if name)

    def validate(self, builder: Any) -> None:
        if (
            not isinstance(self.measure, Measure)
            or self.row_sort not in {"group", "value"}
            or (self.column_sort not in {"group", "value"})
        ):
            raise ImproperlyConfigured("Invalid aggregation configuration.")
        if (
            not isinstance(self.rows, str)
            or not self.rows
            or self.columns == self.rows
            or (
                self.columns is not None and (not isinstance(self.columns, str) or not self.columns)
            )
            or not isinstance(self.row_descending, bool)
            or not isinstance(self.column_descending, bool)
        ):
            raise ImproperlyConfigured("Use one row field and an optional different column field.")
        for name in self.fields:
            spec = builder.specs.get(name)
            if spec is None or name not in builder.fields or isinstance(spec.column.type, sa.JSON):
                raise ImproperlyConfigured("Aggregations require published scalar model columns.")
        if self.measure.field:
            kind = builder.specs[self.measure.field].column.type
            if isinstance(kind, sa.Boolean) or not isinstance(kind, (sa.Integer, sa.Numeric)):
                raise ImproperlyConfigured("Measures require numeric columns, excluding booleans.")


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


@dataclass(frozen=True)
class AggregateResult:
    rows: list[Any]
    columns: list[Any]
    cells: list[list[Any]]
    row_totals: list[Any]
    column_totals: list[Any]
    total: Any
    records: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows": [encode(value) for value in self.rows],
            "columns": [encode(value) for value in self.columns],
            "cells": [[encode(value) for value in row] for row in self.cells],
            "row_totals": [encode(value) for value in self.row_totals],
            "column_totals": [encode(value) for value in self.column_totals],
            "total": encode(self.total),
            "records": self.records,
        }


def exceeded() -> BuilderValidationError:
    return BuilderValidationError(
        {"filter": ["Too many groups. Narrow the filters and try again."]}, code="aggregate_limit"
    )


async def execute(
    statement: Any,
    builder: Any,
    session: Any,
    query: Aggregation,
    *,
    max_rows: int,
    max_columns: int,
    max_cells: int,
) -> AggregateResult:
    if any(
        isinstance(limit, bool) or not isinstance(limit, int) or limit < 1
        for limit in (max_rows, max_columns, max_cells)
    ):
        raise ImproperlyConfigured("Aggregation limits must be positive integers.")
    names = tuple(dict.fromkeys(query.fields))
    selected = [getattr(builder.model, name).label(name) for name in names]
    scope = (
        statement.order_by(None)
        .with_only_columns(builder.pk.label("_aggregate_pk"), *selected, maintain_column_froms=True)
        .distinct()
        .subquery()
    )
    dimensions = [scope.c[query.rows]]
    if query.columns:
        dimensions.append(scope.c[query.columns])
    operation = query.measure.operation
    field = scope.c[query.measure.field] if query.measure.field else None
    value = (
        sa.func.count()
        if field is None
        else getattr(sa.func, "sum" if operation == "avg" else operation)(field)
    )
    group_limit = min(max_cells, max_rows * (max_columns if query.columns else 1))
    statement = (
        sa.select(
            *dimensions,
            sa.func.count(),
            sa.func.count(field) if field is not None else sa.func.count(),
            value,
        )
        .group_by(*dimensions)
        .limit(group_limit + 1)
    )
    records = (await session.execute(statement)).all()
    if len(records) > group_limit:
        raise exceeded()
    cells: dict[tuple[Any, Any], State] = {}
    rows: dict[Any, State] = {}
    columns: dict[Any, State] = {}
    total = State()
    with localcontext() as context:
        context.prec = 38
        for record in records:
            row = record[0]
            column = record[1] if query.columns else None
            state = State(*record[-3:])
            cells[row, column] = state
            rows.setdefault(row, State()).merge(state, operation)
            columns.setdefault(column, State()).merge(state, operation)
            total.merge(state, operation)
        if (
            len(rows) > max_rows
            or len(columns) > max_columns
            or (len(rows) * len(columns) > max_cells)
        ):
            raise exceeded()

        def ordered(items: dict[Any, State], by: str, descending: bool) -> list[Any]:
            keys = [key for key in items if key is not None]
            keys.sort(key=lambda key: key.value if isinstance(key, Enum) else key)
            if by == "value":
                available = [key for key in keys if items[key].result(operation) is not None]
                missing = [key for key in keys if items[key].result(operation) is None]
                available.sort(key=lambda key: items[key].result(operation), reverse=descending)
                keys = available + missing
            elif descending:
                keys.reverse()
            return [*keys, *([None] if None in items else [])]

        row_keys = ordered(rows, query.row_sort, query.row_descending)
        column_keys = ordered(columns, query.column_sort, query.column_descending)
        return AggregateResult(
            row_keys,
            column_keys,
            [
                [cells.get((row, column), State()).result(operation) for column in column_keys]
                for row in row_keys
            ],
            [rows[row].result(operation) for row in row_keys],
            [columns[column].result(operation) for column in column_keys],
            total.result(operation),
            total.records,
        )
