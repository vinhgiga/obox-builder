"""Validated filter trees shared by list views and the ApiOptions."""

from __future__ import annotations

import json
import operator
import re
from collections.abc import Callable
from dataclasses import dataclass
from fnmatch import translate
from typing import Any

import sqlalchemy as sa

from obox.core.exceptions import BuilderValidationError
from obox.db.fields import ModelField, schema, validate

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
    name = term.field
    if name not in builder.filter_fields or name not in readable:
        raise _condition_error(name, "Filtering this field is not permitted.", "unknown_field")
    return compile_predicate(builder.specs[name], getattr(builder.model, name), term, dialect)


def compile_predicate(spec: ModelField, column: Any, term: FilterTerm, dialect: str) -> Any:
    name, op, raw = term.field, term.op.removeprefix("pg."), term.value
    negate, op = op.startswith("not."), op.removeprefix("not.")
    if "obox_files" in spec.column.info:
        if op == "is" and raw == "null":
            return column.any() if negate else ~column.any()
        raise _condition_error(
            name, "Use is.null or not.is.null for attachments.", "unsupported_operator"
        )
    type_ = spec.column.type
    if isinstance(type_, sa.JSON):
        raise _condition_error(name, "JSON fields cannot be filtered.", "unsupported_operator")
    text = isinstance(type_, sa.String) and not isinstance(type_, sa.Enum)
    model = None

    def value(item: Any) -> Any:
        nonlocal model
        try:
            if model is None:
                model = schema("Filter", {spec.name: spec}, (spec.name,), partial=True)
            parsed = validate(model, {spec.name: item})[spec.name]
            return spec.normalize(parsed)
        except BuilderValidationError as exc:
            if name == spec.name:
                raise
            raise BuilderValidationError({name: exc.errors[spec.name]}) from exc
        except (ValueError, OverflowError) as exc:
            raise BuilderValidationError({name: [str(exc)]}) from exc

    if op == "is":
        choices = {"null": None, "true": True, "false": False}
        if raw in choices and (raw == "null" or isinstance(type_, sa.Boolean)):
            result = column.is_(choices[raw])
        else:
            raise _condition_error(
                name,
                "This field does not support the requested is comparison.",
                "unsupported_operator",
            )
    elif op in {"like", "ilike"} and text:
        pieces: list[str] = re.findall(r"\\.|.", raw, flags=re.DOTALL)
        if op == "like" and dialect == "sqlite":
            # fnmatch translates wildcard runs with atomic groups.
            glob = {"*": "*", "%": "*", "_": "?"}
            special = {"[": "[[]", "*": "[*]", "?": "[?]"}
            pattern = "".join(
                glob.get(piece, special.get(piece.removeprefix("\\"), piece.removeprefix("\\")))
                for piece in pieces
            )
            result = column.regexp_match(r"\A" + translate(pattern))
        else:
            result = getattr(column, op)(
                "".join("%" if piece == "*" else piece for piece in pieces), escape="\\"
            )
    elif op == "in":
        values = [] if raw == [""] else [value(item) for item in raw or ()]
        result = column.in_(values)
    elif op == "isdistinct":
        result = column.is_distinct_from(value(raw))
    elif op in COMPARISONS:
        if op not in {"eq", "neq"} and not isinstance(
            type_, sa.Integer | sa.Numeric | sa.Date | sa.DateTime | sa.Time
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
