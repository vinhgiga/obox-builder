"""The supported read-query grammar, SQL predicates, and signed continuation tokens."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import operator
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import sqlalchemy as sa
from pydantic import TypeAdapter

from obox_builder.builder.fields import schema, validate
from obox_builder.builder.filters import MAX_FILTER_DEPTH, MAX_FILTER_NODES, FilterGroup, FilterTerm
from obox_builder.errors import BuilderValidationError

CONTROLS = {"search", "select", "expand", "order", "limit", "offset", "cursor", "count"}
GROUPS = {"and", "or", "not.and", "not.or"}


def parameters(items: Iterable[tuple[str, str]], allowed: Iterable[str] = ()) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, value in items:
        if name not in allowed:
            raise invalid(name, "Unknown query parameter.", "unknown_parameter")
        if name in result:
            raise invalid(name, "Supply this parameter once.")
        result[name] = value
    return result


COMPARISONS = {
    "eq": operator.eq,
    "neq": operator.ne,
    "gt": operator.gt,
    "gte": operator.ge,
    "lt": operator.lt,
    "lte": operator.le,
}


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


def parse_terms(pairs: Iterable[tuple[str, str]]) -> tuple[dict[str, str], FilterGroup]:
    controls: dict[str, str] = {}
    count = 0

    def node(raw: str, depth: int = 0) -> FilterTerm | FilterGroup:
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
        if not name.isidentifier():
            raise invalid(name, "Unknown filter field.", "unknown_field")
        if op == "in":
            if not value.startswith("(") or not value.endswith(")"):
                raise invalid(name, "Use in.(value1,value2).")
            parsed: Any = [literal(part) for part in split_terms(value[1:-1])]
        else:
            parsed = literal(value)
        return FilterTerm(
            name, "pg." + ("not." if expression.startswith("not.") else "") + op, parsed
        )

    nodes = []
    for name, value in pairs:
        if name in CONTROLS:
            if name in controls:
                raise invalid(name, "Supply this parameter once.")
            controls[name] = value
        elif name in GROUPS:
            if not value.startswith("("):
                raise invalid(name, "Use a parenthesized filter group.")
            nodes.append(node(name + value))
        else:
            if name in {"fields", "ordering", "filter"} or name.startswith(
                ("filter.", "filter_op.")
            ):
                raise invalid(name, "Unknown query parameter.", "unknown_parameter")
            nodes.append(node(name + "." + value))
    return controls, FilterGroup(children=tuple(nodes))


def condition(builder: Any, readable: set[str], term: FilterTerm, dialect: str) -> Any:
    name, op, raw = term.field, term.op.removeprefix("pg."), term.value
    if name not in builder.filter_fields or name not in readable:
        raise invalid(name, "Filtering this field is not permitted.", "unknown_field")
    spec, column = builder.specs[name], getattr(builder.model, name)
    negate, op = op.startswith("not."), op.removeprefix("not.")
    if "obox_files" in spec.column.info:
        if op == "is" and raw == "null":
            return column.any() if negate else ~column.any()
        raise invalid(name, "Use is.null or not.is.null for attachments.", "unsupported_operator")
    if isinstance(spec.column.type, sa.JSON):
        raise invalid(name, "JSON fields cannot be filtered.", "unsupported_operator")
    model = schema("Filter", builder.specs, (name,), partial=True)

    def value(item: Any) -> Any:
        return validate(model, {name: item})[name]

    if op == "is":
        choices = {"null": None, "true": True, "false": False}
        if raw not in choices or (raw != "null" and not isinstance(spec.column.type, sa.Boolean)):
            raise invalid(
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
            # SQLite LIKE ignores ASCII case; its registered REGEXP preserves LIKE semantics.
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
        result = column.in_([value(item) for item in raw])
    elif op == "isdistinct":
        result = column.is_distinct_from(value(raw))
    elif op in COMPARISONS:
        if op not in {"eq", "neq"} and not isinstance(
            spec.column.type, sa.Integer | sa.Numeric | sa.Float | sa.Date | sa.DateTime | sa.Time
        ):
            raise invalid(
                name, "This field does not support range comparisons.", "unsupported_operator"
            )
        result = COMPARISONS[op](column, value(raw))
    else:
        raise invalid(name, "Unsupported filter operator.", "unsupported_operator")
    return sa.not_(result) if negate else result


@dataclass
class ListQuery:
    params: dict[str, str]
    filters: FilterGroup
    order: list[tuple[str, bool, bool]] = field(default_factory=list)

    def integer(self, name: str, default: int | None = None) -> int | None:
        raw = self.params.get(name)
        if raw is None:
            return default
        if not re.fullmatch(r"[0-9]+", raw):
            raise invalid(name, "Use a nonnegative integer.")
        try:
            return int(raw)
        except ValueError as exc:
            raise invalid(name, "Invalid integer.") from exc

    def ordering(self, builder: Any, readable: set[str]) -> list[Any]:
        self.order.clear()
        raw = self.params.get("order")
        terms = (
            raw.split(",")
            if raw is not None
            else [
                key.removeprefix("-") + (".desc" if key.startswith("-") else ".asc")
                for key in builder.ordering
                if key.removeprefix("-") in readable or key.removeprefix("-") == builder.pk_name
            ]
        )
        for term in terms:
            parts = term.split(".")
            name, modifiers = parts[0], parts[1:]
            if raw is not None and (name not in builder.sort_fields or name not in readable):
                raise invalid("order", "Sorting this field is not permitted.", "unknown_field")
            direction = modifiers.pop(0) if modifiers and modifiers[0] in {"asc", "desc"} else "asc"
            nulls = (
                modifiers.pop(0)
                if modifiers and modifiers[0] in {"nullsfirst", "nullslast"}
                else ("nullsfirst" if direction == "desc" else "nullslast")
            )
            if modifiers or name in {item[0] for item in self.order}:
                raise invalid("order", "Invalid or duplicate sort term.")
            self.order.append((name, direction == "desc", nulls == "nullsfirst"))
        if builder.pk_name not in {item[0] for item in self.order}:
            self.order.append((builder.pk_name, False, False))
        return [
            (column.desc() if desc else column.asc()).nulls_first()
            if first
            else (column.desc() if desc else column.asc()).nulls_last()
            for name, desc, first in self.order
            for column in (getattr(builder.model, name),)
        ]

    def fingerprint(self, builder: Any) -> str:
        payload = [builder.route_key, self.filters.to_dict(), self.params.get("search"), self.order]
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def cursor(self, builder: Any, secret: bytes, values: list[Any] | None = None) -> Any:
        key = hmac.digest(secret, b"obox.cursor", "sha256")
        if values is not None:
            data = TypeAdapter[Any](Any).dump_json([self.fingerprint(builder), values])
            payload = base64.urlsafe_b64encode(data).rstrip(b"=")
            return (payload + b"." + hmac.digest(key, payload, "sha256").hex().encode()).decode()
        try:
            payload, signature = self.params["cursor"].encode("ascii").split(b".")
            if not hmac.compare_digest(
                signature, hmac.digest(key, payload, "sha256").hex().encode()
            ):
                raise ValueError
            fingerprint, values = json.loads(
                base64.b64decode(
                    payload + b"=" * (-len(payload) % 4), altchars=b"-_", validate=True
                )
            )
            if (
                fingerprint != self.fingerprint(builder)
                or not isinstance(values, list)
                or len(values) != len(self.order)
            ):
                raise ValueError
            parsed = validate(
                schema("Cursor", builder.specs, (name for name, _, _ in self.order)),
                dict(zip((name for name, _, _ in self.order), values, strict=True)),
            )
        except (ValueError, TypeError, UnicodeError, binascii.Error, BuilderValidationError) as exc:
            raise invalid("cursor", "Invalid cursor or changed query.", "invalid_cursor") from exc
        conditions = []
        prefix: list[Any] = []
        for name, desc, first in self.order:
            column, last = getattr(builder.model, name), parsed[name]
            comparable = (
                sa.cast(column, sa.Integer)
                if isinstance(builder.specs[name].column.type, sa.Boolean)
                else column
            )
            comparison = int(last) if isinstance(last, bool) else last
            if last is None:
                after = column.is_not(None) if first else sa.false()
            else:
                after = comparable < comparison if desc else comparable > comparison
                if not first:
                    after = sa.or_(after, column.is_(None))
            conditions.append(sa.and_(*prefix, after))
            prefix.append(column == last)
        return sa.or_(*conditions)
