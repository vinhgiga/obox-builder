"""Validated filter trees shared by list views and the ApiOptions."""

from __future__ import annotations

import json
import operator
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa

from obox.core.exceptions import BuilderValidationError
from obox.db.fields import schema, validate

MAX_FILTER_DEPTH = 3
MAX_FILTER_NODES = 50
MAX_FILTER_LENGTH = 32_000


@dataclass(frozen=True)
class FilterTerm:
    field: str
    op: str
    value: Any


@dataclass(frozen=True)
class FilterGroup:
    conjunction: str = "and"
    children: tuple[FilterTerm | FilterGroup, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "conjunction": self.conjunction,
            "children": [
                child.to_dict()
                if isinstance(child, FilterGroup)
                else {"field": child.field, "op": child.op, "value": child.value}
                for child in self.children
            ],
        }


def parse_filter(raw: str) -> FilterGroup | None:
    if not raw:
        return None
    if len(raw) > MAX_FILTER_LENGTH:
        raise _invalid("Filter configuration is too large.")
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise _invalid("Invalid filter configuration.") from exc
    return _group(data, 1, [0])


def _group(raw: Any, depth: int, count: list[int]) -> FilterGroup:
    if (
        depth > MAX_FILTER_DEPTH
        or not isinstance(raw, dict)
        or set(raw) != {"conjunction", "children"}
        or raw["conjunction"] not in {"and", "or"}
        or not isinstance(raw["children"], list)
    ):
        raise _invalid("Invalid filter group.")
    children: list[FilterTerm | FilterGroup] = []
    for child in raw["children"]:
        count[0] += 1
        if count[0] > MAX_FILTER_NODES:
            raise _invalid("Too many filter conditions.")
        if not isinstance(child, dict):
            raise _invalid("Invalid filter condition.")
        if "children" in child:
            children.append(_group(child, depth + 1, count))
        elif (
            set(child) == {"field", "op", "value"}
            and isinstance(child["field"], str)
            and isinstance(child["op"], str)
        ):
            children.append(FilterTerm(child["field"], child["op"], child["value"]))
        else:
            raise _invalid("Invalid filter condition.")
    return FilterGroup(raw["conjunction"], tuple(children))


def compile_filter(
    node: FilterGroup | FilterTerm,
    compile_term: Callable[[FilterTerm], Any | None],
) -> tuple[FilterGroup | FilterTerm | None, Any | None, int]:
    """Compile a filter tree, pruning terms whose compiler returns ``None``."""
    if isinstance(node, FilterTerm):
        expression = compile_term(node)
        return (node, expression, 1) if expression is not None else (None, None, 0)
    compiled = [compile_filter(child, compile_term) for child in node.children]
    active = [
        (clean, expression, count)
        for clean, expression, count in compiled
        if clean is not None and expression is not None
    ]
    if not active:
        return None, None, 0
    children = tuple(clean for clean, _, _ in active if clean is not None)
    conjunction = node.conjunction.removeprefix("not.")
    combine = sa.and_ if conjunction == "and" else sa.or_
    expression = combine(*(expression for _, expression, _ in active))
    if node.conjunction.startswith("not."):
        expression = sa.not_(expression)
    return FilterGroup(node.conjunction, children), expression, sum(count for _, _, count in active)


COMPARISONS = {
    "eq": operator.eq,
    "neq": operator.ne,
    "gt": operator.gt,
    "gte": operator.ge,
    "lt": operator.lt,
    "lte": operator.le,
}


def compile_condition(builder: Any, readable: set[str], term: FilterTerm, dialect: str) -> Any:
    """Compile a PostgreSQL-style term against a component's authorized fields."""
    name, op, raw = term.field, term.op.removeprefix("pg."), term.value
    if name not in builder.filter_fields or name not in readable:
        raise _condition_error(name, "Filtering this field is not permitted.", "unknown_field")
    spec, column = builder.specs[name], getattr(builder.model, name)
    negate, op = op.startswith("not."), op.removeprefix("not.")
    if "obox_files" in spec.column.info:
        if op == "is" and raw == "null":
            return column.any() if negate else ~column.any()
        raise _condition_error(
            name, "Use is.null or not.is.null for attachments.", "unsupported_operator"
        )
    if isinstance(spec.column.type, sa.JSON):
        raise _condition_error(name, "JSON fields cannot be filtered.", "unsupported_operator")
    model = schema("Filter", builder.specs, (name,), partial=True)

    def value(item: Any) -> Any:
        return spec.normalize(validate(model, {name: item})[name])

    if op == "is":
        choices = {"null": None, "true": True, "false": False}
        if raw not in choices or (raw != "null" and not isinstance(spec.column.type, sa.Boolean)):
            raise _condition_error(
                name, "Use is.null or a boolean is.true/is.false.", "unsupported_operator"
            )
        result = column.is_(choices[raw])
    elif (
        op in {"like", "ilike"}
        and isinstance(spec.column.type, sa.String)
        and not isinstance(spec.column.type, sa.Enum)
    ):
        pieces = re.findall(r"\\.|.", raw, flags=re.DOTALL)
        if op == "like" and dialect == "sqlite":
            pattern = (
                "(?s)\\A"
                + "".join(
                    ".*"
                    if piece in {"*", "%"}
                    else "."
                    if piece == "_"
                    else re.escape(piece[1:] if piece.startswith("\\") else piece)
                    for piece in pieces
                )
                + "\\Z"
            )
            result = column.regexp_match(pattern)
        else:
            result = getattr(column, op)(
                "".join("%" if piece == "*" else piece for piece in pieces), escape="\\"
            )
    elif op == "in":
        values = [] if not raw or raw == [""] else [value(item) for item in raw]
        result = column.in_(values)
    elif op == "isdistinct":
        result = column.is_distinct_from(value(raw))
    elif op in COMPARISONS:
        if op not in {"eq", "neq"} and not isinstance(
            spec.column.type, sa.Integer | sa.Numeric | sa.Float | sa.Date | sa.DateTime | sa.Time
        ):
            raise _condition_error(
                name, "This field does not support range comparisons.", "unsupported_operator"
            )
        result = COMPARISONS[op](column, value(raw))
    else:
        raise _condition_error(name, "Unsupported filter operator.", "unsupported_operator")
    return sa.not_(result) if negate else result


def _condition_error(name: str, message: str, code: str) -> BuilderValidationError:
    return BuilderValidationError({name: [message]}, code=code)


def _invalid(message: str) -> BuilderValidationError:
    return BuilderValidationError({"filter": [message]})
