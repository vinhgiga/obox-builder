"""Validated filter trees shared by list views and the API."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from obox_builder.errors import BuilderValidationError

MAX_FILTER_DEPTH = 3
MAX_FILTER_NODES = 100
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


def _invalid(message: str) -> BuilderValidationError:
    return BuilderValidationError({"filter": [message]})
