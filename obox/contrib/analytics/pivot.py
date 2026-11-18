"""Group and subtotal pivot data independently of the data source."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, localcontext
from enum import Enum
from typing import Any, Literal

import sqlalchemy as sa

from obox._exceptions import BuilderValidationError
from obox.contrib.analytics.aggregates import Aggregation, Measure, State, encode, exceeded
from obox.core.exceptions import ImproperlyConfigured

Member = tuple[str, Any]
Path = tuple[Member, ...]
SortOrder = Literal["group", "value"]
ValuesAxis = Literal["rows", "columns"]


def axis(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value else ()
    if isinstance(value, (tuple, list)) and all(isinstance(name, str) and name for name in value):
        return tuple(value)
    raise ImproperlyConfigured("Pivot axes require ordered field names.")


def member(value: Any) -> Member:
    if isinstance(value, Enum):
        value = value.value
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        number = Decimal(str(value))
        if not number.is_finite():
            raise BuilderValidationError({"pivot": ["Non-finite group keys are unsupported."]})
        return "number", number
    return type(value).__name__, encode(value)


@dataclass(frozen=True)
class PivotSpec:
    rows: tuple[str, ...]
    columns: tuple[str, ...]
    values: tuple[Measure, ...]
    values_axis: ValuesAxis
    values_position: int
    row_sort: SortOrder
    column_sort: SortOrder
    row_descending: bool
    column_descending: bool
    show_row_subtotals: bool
    show_column_subtotals: bool
    show_row_totals: bool
    show_column_totals: bool
    legacy: Aggregation | None

    @property
    def fields(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys((*self.rows, *self.columns, *(v.field for v in self.values if v.field)))
        )

    def validate_model(self, builder: Any) -> None:
        for name in self.fields:
            field = builder.specs.get(name)
            if (
                field is None
                or name not in builder.fields
                or isinstance(field.column.type, sa.JSON)
            ):
                raise ImproperlyConfigured("Pivot requires published scalar model columns.")
        for value in self.values:
            if value.operation in {"sum", "avg", "min", "max"}:
                field_type = builder.specs[value.field].column.type
                if not numeric_column(field_type):
                    raise ImproperlyConfigured("Pivot numeric aggregates require numeric columns.")

    def selection(self, query: Any) -> dict[str, Member]:
        keys = {}
        for name, fields in (("row", self.rows), ("column", self.columns)):
            if name not in query.model_fields_set:
                continue
            path = getattr(query, name)
            if (
                not isinstance(path, list)
                or len(path) > len(fields)
                or any(
                    not isinstance(part, list)
                    or len(part) != 2
                    or not isinstance(part[0], str)
                    or isinstance(part[1], (list, dict))
                    for part in path
                )
            ):
                raise BuilderValidationError({name: ["Use a pivot group path."]})
            for field, (kind, value) in zip(fields, path, strict=False):
                if kind == "number":
                    try:
                        value = Decimal(str(value))
                        if not value.is_finite():
                            raise ValueError
                    except (ArithmeticError, ValueError) as exc:
                        raise BuilderValidationError(
                            {name: ["Invalid numeric group key."]}
                        ) from exc
                keys[field] = kind, value
        return keys


def numeric_column(field_type: Any) -> bool:
    return isinstance(field_type, (sa.Integer, sa.Numeric)) and not isinstance(
        field_type, sa.Boolean
    )


def measure_result(state: State, measure: Measure) -> Any:
    if measure.operation == "count":
        return (state.present or None) if measure.field else state.records
    if measure.operation == "count_numbers":
        return state.present if state.records else None
    return state.result(measure.operation)


def measure_label(value: Measure) -> str:
    operation = {
        "count": "Count",
        "count_numbers": "Count Numbers",
        "sum": "Sum",
        "avg": "Average",
        "min": "Min",
        "max": "Max",
    }[value.operation]
    return value.label or (f"{operation} of {value.field}" if value.field else operation)


class Matrix:
    def __init__(self, spec: PivotSpec, *, max_rows: int, max_columns: int, max_cells: int) -> None:
        if any(type(limit) is not int or limit < 1 for limit in (max_rows, max_columns, max_cells)):
            raise ImproperlyConfigured("Pivot limits must be positive integers.")
        if len(spec.values) > max_cells:
            raise exceeded()
        self.spec = spec
        self.max_rows, self.max_columns, self.max_cells = max_rows, max_columns, max_cells
        self.leaves: dict[tuple[Path, Path], list[State]] = {}
        self.rows: set[Path] = {()}
        self.columns: set[Path] = {()}
        self.records = 0

    def add(self, row: Path, column: Path, states: list[State], records: int = 1) -> None:
        self.records += records
        key = row, column
        target = self.leaves.get(key)
        if target is not None:
            self._merge(target, states)
            return
        self.rows.update(row[:i] for i in range(1, len(row) + 1))
        self.columns.update(column[:i] for i in range(1, len(column) + 1))
        if (
            max(1, len(self.rows) - 1) > self.max_rows
            or max(1, len(self.columns) - 1) > self.max_columns
            or len(self.rows) * len(self.columns) * len(self.spec.values) > self.max_cells
        ):
            raise exceeded()
        self.leaves[key] = states

    def _merge(self, target: list[State], states: list[State]) -> None:
        for current, state, measure in zip(target, states, self.spec.values, strict=True):
            current.merge(state, measure.operation)

    def result(self) -> dict[str, Any]:
        spec = self.spec
        empty = [State() for _ in spec.values]
        buckets: dict[tuple[Path, Path], list[State]] = defaultdict(
            lambda: [State() for _ in spec.values]
        )
        for (row, column), states in self.leaves.items():
            for r in range(len(row) + 1):
                for c in range(len(column) + 1):
                    self._merge(buckets[row[:r], column[:c]], states)

        def values(row: Path, column: Path) -> list[Any]:
            states = buckets.get((row, column))
            if states is None and (row or column or spec.rows or spec.columns):
                return [None] * len(spec.values)
            return [
                encode(measure_result(state, measure))
                for state, measure in zip(states or empty, spec.values, strict=True)
            ]

        def ordered(paths: set[Path], *, is_row: bool) -> list[Path]:
            if not self.records:
                return []
            if len(paths) == 1:
                return [()]
            groups: dict[Path, list[Path]] = defaultdict(list)
            for path in paths:
                if path:
                    groups[path[:-1]].append(path)
            order = spec.row_sort if is_row else spec.column_sort
            descending = spec.row_descending if is_row else spec.column_descending
            ranks: dict[Path, int] = {}
            for siblings in groups.values():
                populated = [path for path in siblings if path[-1][1] is not None]
                populated.sort(key=lambda path: member_order(path[-1]), reverse=descending)
                populated.extend(path for path in siblings if path[-1][1] is None)
                if order == "value":
                    scores = {
                        path: measure_result(
                            buckets[path, ()][0] if is_row else buckets[(), path][0],
                            spec.values[0],
                        )
                        for path in populated
                    }
                    available = [path for path in populated if scores[path] is not None]
                    available.sort(key=scores.__getitem__, reverse=descending)
                    populated = available + [path for path in populated if scores[path] is None]
                ranks.update((path, index) for index, path in enumerate(populated))
            return sorted(
                ranks, key=lambda path: tuple(ranks[path[:i]] for i in range(1, len(path) + 1))
            )

        def keys(paths: list[Path]) -> list[list[Member]]:
            return [[(kind, encode(value)) for kind, value in path] for path in paths]

        rows = ordered(self.rows, is_row=True)
        columns = ordered(self.columns, is_row=False)
        return {
            "row_fields": list(spec.rows),
            "column_fields": list(spec.columns),
            "values": [measure_label(value) for value in spec.values],
            "values_axis": spec.values_axis,
            "values_position": spec.values_position,
            "show_row_subtotals": spec.show_row_subtotals,
            "show_column_subtotals": spec.show_column_subtotals,
            "show_row_totals": spec.show_row_totals,
            "show_column_totals": spec.show_column_totals,
            "rows": keys(rows),
            "columns": keys(columns),
            "cells": [[values(row, column) for column in columns] for row in rows],
            "row_totals": [values(row, ()) for row in rows],
            "column_totals": [values((), column) for column in columns],
            "total": values((), ()),
            "records": self.records,
        }


def member_order(key: Member) -> tuple[Any, ...]:
    kind, value = key
    return (kind, value.casefold(), value) if isinstance(value, str) else (kind, value)


async def execute_sql(
    statement: Any, builder: Any, session: Any, spec: PivotSpec, **limits: int
) -> dict[str, Any]:
    names = spec.fields
    selected = [getattr(builder.model, name).label(name) for name in names]
    scope = (
        statement.order_by(None)
        .with_only_columns(builder.pk.label(None), *selected, maintain_column_froms=True)
        .distinct()
        .subquery()
    )
    fields = (*spec.rows, *spec.columns)
    dimensions = [scope.c[name] for name in fields]
    values: list[Any] = []
    for measure in spec.values:
        column = scope.c[measure.field] if measure.field else None
        operation = measure.operation
        if column is None:
            values.extend((sa.func.count(), sa.null(), sa.null()))
        elif operation == "count":
            values.extend((sa.func.count(), sa.func.count(column), sa.null()))
        elif operation == "count_numbers":
            nonempty = sa.func.count(column)
            numeric = numeric_column(builder.specs[measure.field].column.type)
            values.extend((nonempty, nonempty if numeric else sa.literal(0), sa.null()))
        else:
            aggregate = getattr(sa.func, "sum" if operation == "avg" else operation)
            values.extend((sa.func.count(), sa.func.count(column), aggregate(column)))
    matrix = Matrix(spec, **limits)
    bound = min(matrix.max_cells // len(spec.values), matrix.max_rows * matrix.max_columns)
    query = sa.select(*dimensions, sa.func.count(), *values).select_from(scope)
    if dimensions:
        query = query.group_by(*dimensions)
    records = (await session.execute(query.limit(bound + 1))).all()
    if len(records) > bound:
        raise exceeded()
    with localcontext() as context:
        context.prec = 38
        for record in records:
            keys = tuple(member(value) for value in record[: len(fields)])
            count = record[len(fields)]
            if count:
                items = record[len(fields) + 1 :]
                states = [
                    State(items[i], items[i + 1] or 0, items[i + 2])
                    for i in range(0, len(items), 3)
                ]
                matrix.add(keys[: len(spec.rows)], keys[len(spec.rows) :], states, count)
        return matrix.result()
