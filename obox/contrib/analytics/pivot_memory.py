"""Tabular data and filtering for pivots without a model."""

from __future__ import annotations

import math
import operator
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import date, time
from decimal import Decimal, localcontext
from enum import Enum
from types import MappingProxyType
from typing import Any, cast
from uuid import UUID

from pydantic import TypeAdapter, ValidationError

from obox._exceptions import BuilderValidationError
from obox._filters import FilterGroup, FilterTerm
from obox.contrib.analytics.aggregates import State
from obox.contrib.analytics.pivot import Matrix, Member, PivotSpec, member
from obox.core.exceptions import ImproperlyConfigured

Row = Mapping[str, Any]
Predicate = Callable[[Row], bool]


def scalar(value: Any) -> Any:
    kind = type(value)
    if kind.__module__.startswith("pandas.") and kind.__name__ in {"NAType", "NaTType"}:
        return None
    if kind.__module__.startswith("numpy") and getattr(value, "ndim", None) == 0:
        value = value.item()
    if isinstance(value, Enum):
        value = value.value
    if (isinstance(value, float) and math.isnan(value)) or (
        isinstance(value, Decimal) and value.is_nan()
    ):
        return None
    if value is None or isinstance(value, (str, bool, int, float, Decimal, date, time, UUID)):
        return value
    raise ImproperlyConfigured("Pivot data requires scalar values in published fields.")


@dataclass(frozen=True)
class MemoryRows:
    columns: tuple[str, ...]
    rows: tuple[Row, ...]

    def __deepcopy__(self, memo: dict[int, Any]) -> MemoryRows:
        return self

    @classmethod
    def from_data(cls, data: Any, fields: tuple[str, ...]) -> MemoryRows:
        if isinstance(data, cls) and (not fields or fields == data.columns):
            return data
        source: Iterable[Row]
        headers: Iterable[str] = ()
        itertuples = getattr(data, "itertuples", None)
        iter_rows = getattr(data, "iter_rows", None)
        if callable(itertuples):
            columns = tuple(data.columns)
            if len(set(columns)) != len(columns):
                raise ImproperlyConfigured("Pivot DataFrame columns must be unique.")
            headers = columns
            source = (
                dict(zip(columns, values, strict=True))
                for values in cast(Iterable[tuple[Any, ...]], itertuples(index=False, name=None))
            )
        elif callable(iter_rows):
            headers = tuple(data.columns)
            source = cast(Iterable[Row], iter_rows(named=True))
        elif isinstance(data, Iterable) and not isinstance(data, (str, bytes, Mapping)):
            source = data
        else:
            raise ImproperlyConfigured("Pivot data requires records or a DataFrame.")
        names = dict.fromkeys(headers)
        records = []
        for row in source:
            if not isinstance(row, Mapping):
                raise ImproperlyConfigured("Pivot records require field mappings.")
            names.update(dict.fromkeys(row))
            records.append(
                MappingProxyType({name: scalar(row.get(name)) for name in fields or row.keys()})
            )
        if names and set(fields) - names.keys():
            raise ImproperlyConfigured("Pivot data is missing declared fields.")
        published = fields or tuple(names)
        if any(not isinstance(name, str) or not name for name in published):
            raise ImproperlyConfigured("Pivot fields must be nonempty strings.")
        return cls(published, tuple(records))

    def matching(
        self,
        *,
        readable: set[str],
        filter_fields: tuple[str, ...],
        fixed: FilterGroup,
        filter_group: FilterGroup | None = None,
        search: str | None = None,
        keys: Mapping[str, Member] | None = None,
    ) -> Iterator[Row]:
        available = readable.intersection(filter_fields)
        predicates = [
            predicate
            for group in (fixed, filter_group)
            if group is not None and (predicate := condition(group, available)) is not None
        ]
        term = search.casefold() if search else ""
        return (
            row
            for row in self.rows
            if all(predicate(row) for predicate in predicates)
            and (not keys or all(member(row.get(name)) == key for name, key in keys.items()))
            and (
                not term
                or any(
                    isinstance(value, str) and term in value.casefold()
                    for name, value in row.items()
                    if name in readable
                )
            )
        )


def condition(node: FilterGroup | FilterTerm, available: set[str]) -> Predicate | None:
    if isinstance(node, FilterGroup):
        children = [
            check for child in node.children if (check := condition(child, available)) is not None
        ]
        if not children:
            return None
        reduce = all if node.conjunction.removeprefix("not.") == "and" else any
        negate = node.conjunction.startswith("not.")
        return lambda row: reduce(check(row) for check in children) != negate

    field, op, operand = node.field, node.op, node.value
    if field not in available:
        raise BuilderValidationError({field: ["Filtering this field is not permitted."]})
    negate = op.startswith("pg.not.")
    op = op.removeprefix("pg.").removeprefix("not.")
    if node.op.startswith("pg.") and op == "is":
        choices = {"null": None, "true": True, "false": False}
        if not isinstance(operand, str) or operand not in choices:
            raise BuilderValidationError({field: ["Use is.null, is.true or is.false."]})
        operand = choices[operand]
    if op in {"is_empty", "is_not_empty"}:
        return lambda row: (row.get(field) in (None, "")) == (op == "is_empty")
    if op in {"contains", "does_not_contain"} and isinstance(operand, str):
        text = operand.casefold()

        def contains(row: Row) -> bool:
            value = row.get(field)
            return (isinstance(value, str) and text in value.casefold()) == (op == "contains")

        return contains
    operations: dict[str, Callable[[Any, Any], bool]] = {
        "is": operator.eq,
        "is_not": operator.ne,
        "eq": operator.eq,
        "neq": operator.ne,
        "isdistinct": operator.ne,
        "lt": operator.lt,
        "lte": operator.le,
        "gt": operator.gt,
        "gte": operator.ge,
    }
    if op not in operations and op != "in":
        raise BuilderValidationError({field: ["Unsupported in-memory filter operator."]})

    def compare(row: Row) -> bool:
        value = row.get(field)

        def convert(raw: Any) -> Any:
            if isinstance(raw, str) and value is not None and not isinstance(value, str):
                return TypeAdapter(type(value)).validate_python(raw)
            return raw

        try:
            if op == "in":
                result = any(value == convert(item) for item in operand)
            else:
                result = bool(operations[op](value, convert(operand)))
        except (ValidationError, TypeError):
            return False
        return result != negate

    return compare


def execute_memory(rows: Iterable[Row], spec: PivotSpec, **limits: int) -> dict[str, Any]:
    matrix = Matrix(spec, **limits)
    with localcontext() as context:
        context.prec = 38
        for row in rows:
            states = []
            for measure in spec.values:
                value = row.get(measure.field) if measure.field else None
                nonempty = int(value is not None)
                numeric = isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)
                if measure.operation == "count":
                    states.append(State(1, nonempty))
                elif measure.operation == "count_numbers":
                    states.append(State(nonempty, int(numeric)))
                else:
                    # Spreadsheet sums ignore text and booleans, but a populated group is zero.
                    number = Decimal(str(value)) if isinstance(value, float) else value
                    if not numeric:
                        number = None
                    if measure.operation == "sum" and number is None and nonempty:
                        number = Decimal(0)
                    states.append(State(1, int(numeric), number))
            matrix.add(
                tuple(member(row.get(name)) for name in spec.rows),
                tuple(member(row.get(name)) for name in spec.columns),
                states,
            )
        return matrix.result()
