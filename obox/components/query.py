"""The supported read-query grammar, SQL predicates, and signed continuation tokens."""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from typing import Any

from obox.core.exceptions import BuilderValidationError
from obox.db.filters import (
    MAX_FILTER_DEPTH,
    MAX_FILTER_NODES,
    FilterGroup,
    FilterTerm,
)

CONTROLS = {"search", "select", "expand", "order", "limit", "offset", "cursor", "count"}
GROUPS = {"and", "or", "not.and", "not.or"}


def parameters(items: Iterable[tuple[str, str]], allowed: Iterable[str] = ()) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, value in items:
        if name not in allowed:
            raise invalid(name or "query", "Unknown query parameter.", "unknown_parameter")
        if name in result:
            raise invalid(name, "Supply this parameter once.")
        result[name] = value
    return result


def invalid(name: str, message: str, code: str = "validation_error") -> BuilderValidationError:
    return BuilderValidationError({name: [message]}, code=code)


def split_terms(raw: str) -> list[str]:
    """Split commas outside quoted values and nested groups; never decode URLs here."""
    parts, start, depth, quoted, escaped = [], 0, 0, False, False
    for index, char in enumerate(raw):
        if escaped:
            escaped = False
        elif quoted and char == "\\":
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif not quoted:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth < 0:
                    raise invalid("filter", "Unbalanced filter parentheses.")
            elif char == "," and depth == 0:
                parts.append(raw[start:index])
                start = index + 1
    parts.append(raw[start:])
    if quoted or escaped or depth or any(not part for part in parts):
        raise invalid("filter", "Invalid query expression.")
    return parts


def literal(raw: str) -> str:
    if raw.startswith('"'):
        if not re.fullmatch(r'"(?:[^"\\]|\\["\\])*"', raw):
            raise invalid("filter", "Invalid quoted value.")
        return re.sub(r'\\(["\\])', r"\1", raw[1:-1])
    if any(char in raw for char in '",()'):
        raise invalid("filter", "Quote reserved characters in filter values.")
    return raw


def _parse_nodes(terms: Iterable[str]) -> FilterGroup:
    count = 0

    def node(raw: str, depth: int) -> FilterTerm | FilterGroup:
        nonlocal count
        count += 1
        if count > MAX_FILTER_NODES:
            raise invalid("filter", "Too many filter conditions.")
        group = re.match(r"^(not\.)?(and|or)\(", raw)
        if group:
            if depth >= MAX_FILTER_DEPTH or not raw.endswith(")"):
                raise invalid("filter", "Invalid filter group or nesting depth.")
            return FilterGroup(
                raw[: group.end() - 1],
                tuple(node(part, depth + 1) for part in split_terms(raw[group.end() : -1])),
            )
        try:
            name, expression = raw.split(".", 1)
            op, value = expression.removeprefix("not.").split(".", 1)
        except ValueError as exc:
            raise invalid("filter", "Use field.operator.value.") from exc
        if not name or not op:
            raise invalid("filter", "Use field.operator.value.")
        if not name.isidentifier():
            raise invalid(name, "Unknown filter field.", "unknown_field")
        if op == "in":
            if not value.startswith("(") or not value.endswith(")"):
                raise invalid(name, "Use in.(value1,value2).")
            parts = [] if value == "()" else split_terms(value[1:-1])
            parsed: Any = [literal(part) for part in parts]
        else:
            parsed = literal(value)
        return FilterTerm(
            name, "pg." + ("not." if expression.startswith("not.") else "") + op, parsed
        )

    return FilterGroup(children=tuple(node(term, 0) for term in terms))


def parse_terms(pairs: Iterable[tuple[str, str]]) -> tuple[dict[str, str], FilterGroup]:
    controls: dict[str, str] = {}

    def terms() -> Iterator[str]:
        for name, value in pairs:
            if name in CONTROLS:
                if name in controls:
                    raise invalid(name, "Supply this parameter once.")
                controls[name] = value
            elif name in GROUPS:
                if not value.startswith("("):
                    raise invalid(name, "Use a parenthesized filter group.")
                yield name + value
            else:
                if name in {"fields", "ordering", "filter"} or name.startswith(
                    ("filter.", "filter_op.")
                ):
                    raise invalid(name, "Unknown query parameter.", "unknown_parameter")
                yield name + "." + value

    return controls, _parse_nodes(terms())


def integer(params: dict[str, str], name: str, default: int | None = None) -> int | None:
    raw = params.get(name)
    if raw is None:
        return default
    if not re.fullmatch(r"[0-9]+", raw):
        raise invalid(name, "Use a nonnegative integer.")
    try:
        return int(raw)
    except ValueError as exc:
        raise invalid(name, "Invalid integer.") from exc
