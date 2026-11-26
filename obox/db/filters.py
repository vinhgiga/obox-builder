"""Validated filter trees shared by list views and the ApiOptions."""

from __future__ import annotations

import json
import operator
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from fnmatch import translate
from typing import Any

import sqlalchemy as sa

from obox.core.exceptions import BuilderValidationError
from obox.db.fields import ModelField, schema, validate, validate_text

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

_DATE = r"[0-9]{4}-[0-9]{2}-[0-9]{2}"
_TIME = r"[0-9]{2}:[0-9]{2}:[0-9]{2}"
_TIMESTAMP = rf"{_DATE}[Tt]{_TIME}(?:\.[0-9]+)?(?:[Zz]|[+-][0-9]{{2}}:[0-9]{{2}})"


def compile_condition(builder: Any, readable: set[str], term: FilterTerm, dialect: str) -> Any:
    """Compile a PostgreSQL-style term against a component's authorized fields."""
    name = term.field
    if name not in builder.filter_fields or name not in readable:
        raise _condition_error(name, "Filtering this field is not permitted.", "unknown_field")
    return compile_predicate(builder.specs[name], getattr(builder.model, name), term, dialect)


def compile_predicate(
    spec: ModelField, column: Any, term: FilterTerm, dialect: str, *, rest: bool = False
) -> Any:
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
            if rest:
                if isinstance(type_, sa.DateTime):
                    if re.fullmatch(_DATE, item):
                        return date.fromisoformat(item)
                    if not re.fullmatch(_TIMESTAMP, item):
                        raise ValueError("Use an RFC 3339 timestamp with an offset or YYYY-MM-DD.")
                elif isinstance(type_, sa.Date) and not re.fullmatch(_DATE, item):
                    raise ValueError("Use YYYY-MM-DD.")
                elif isinstance(type_, sa.Time) and not re.fullmatch(
                    rf"(?:[0-9]{{2}}:[0-9]{{2}}|{_TIME}(?:\.[0-9]{{1,6}})?)", item
                ):
                    raise ValueError("Use HH:MM or HH:MM:SS with optional microseconds.")
                elif (
                    isinstance(type_, sa.Enum)
                    and type_.enum_class is None
                    and item not in type_.enums
                ):
                    raise ValueError("Use a declared enum value.")
            if model is None:
                model = schema("Filter", {spec.name: spec}, (spec.name,), partial=True)
            parsed = validate(model, {spec.name: item})[spec.name]
            if rest and isinstance(type_, sa.Time) and type_.timezone:
                parsed = parsed.replace(tzinfo=UTC)
            return spec.normalize(parsed)
        except BuilderValidationError as exc:
            if name == spec.name:
                raise
            raise BuilderValidationError({name: exc.errors[spec.name]}) from exc
        except (ValueError, OverflowError) as exc:
            raise BuilderValidationError({name: [str(exc)]}) from exc

    if op == "is":
        choices = {"null": None, "true": True, "false": False}
        if rest and raw == "empty" and text:
            result = sa.or_(column.is_(None), column == "")
        elif raw in choices and (raw == "null" or isinstance(type_, sa.Boolean)):
            result = column.is_(choices[raw])
        else:
            if rest and raw not in {*choices, "empty"}:
                raise BuilderValidationError(
                    {name: ["Use is.null, is.empty, is.true or is.false."]}
                )
            raise _condition_error(
                name,
                "This field does not support the requested is comparison.",
                "unsupported_operator",
            )
    elif op in {"like", "ilike"} and text:
        pieces: list[str] = re.findall(r"\\.|.", raw, flags=re.DOTALL)
        if rest:
            try:
                validate_text(raw)
                if pieces and pieces[-1] == "\\":
                    raise ValueError("Escape the final backslash in the pattern.")
            except ValueError as exc:
                raise BuilderValidationError({name: [str(exc)]}) from exc
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
        if rest and isinstance(type_, sa.Boolean):
            raise _condition_error(name, "This field does not support in.", "unsupported_operator")
        values = [] if raw == [""] and not rest else [value(item) for item in raw or ()]
        if rest and isinstance(type_, sa.DateTime):
            days, instants = [], []
            for item in values:
                if isinstance(item, date) and not isinstance(item, datetime):
                    days.append(_day_comparison(spec, column, "eq", item))
                else:
                    instants.append(item)
            result = sa.or_(*days, column.in_(instants)) if instants or not days else sa.or_(*days)
        else:
            result = column.in_(values)
    elif op == "isdistinct":
        parsed = value(raw)
        if rest and isinstance(type_, sa.DateTime) and not isinstance(parsed, datetime):
            result = sa.or_(column.is_(None), _day_comparison(spec, column, "neq", parsed))
        else:
            result = column.is_distinct_from(parsed)
    elif op in COMPARISONS:
        if op not in {"eq", "neq"} and not isinstance(
            type_, sa.Integer | sa.Numeric | sa.Date | sa.DateTime | sa.Time
        ):
            raise _condition_error(
                name, "This field does not support range comparisons.", "unsupported_operator"
            )
        parsed = value(raw)
        result = (
            _day_comparison(spec, column, op, parsed)
            if rest and isinstance(type_, sa.DateTime) and not isinstance(parsed, datetime)
            else COMPARISONS[op](column, parsed)
        )
    else:
        raise _condition_error(name, "Unsupported filter operator.", "unsupported_operator")
    return sa.not_(result) if negate else result


def _day_comparison(spec: ModelField, column: Any, op: str, day: date) -> Any:
    start = spec.normalize(datetime.combine(day, time.min, UTC))
    last = day == date.max
    end = spec.normalize(
        datetime.max.replace(tzinfo=UTC)
        if last
        else datetime.combine(day + timedelta(days=1), time.min, UTC)
    )
    if op == "gte":
        return column >= start
    if op == "lt":
        return column < start
    before_end = column <= end if last else column < end
    if op == "lte":
        return before_end
    if op == "gt":
        return sa.not_(before_end)
    inside = sa.and_(column >= start, before_end)
    return sa.not_(inside) if op == "neq" else inside


def _condition_error(name: str, message: str, code: str) -> BuilderValidationError:
    return BuilderValidationError({name: [message]}, code=code)


def _invalid(message: str) -> BuilderValidationError:
    return BuilderValidationError({"filter": [message]})
