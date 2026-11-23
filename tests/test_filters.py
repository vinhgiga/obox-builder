from __future__ import annotations

import json

import pytest
import sqlalchemy as sa
from obox.components.query import parse_terms
from obox.core.exceptions import BuilderValidationError
from obox.db.filters import FilterGroup, FilterTerm, compile_filter, parse_filter


def group(*children: dict[str, object], conjunction: str = "and") -> dict[str, object]:
    return {"conjunction": conjunction, "children": list(children)}


def term(index: int) -> dict[str, object]:
    return {"field": f"field_{index}", "op": "is", "value": index}


def test_filter_tree_uses_shared_depth_and_node_limits() -> None:
    three_levels = group(group(group(term(1))))
    four_levels = group(group(group(group(term(1)))))
    assert parse_filter(json.dumps(three_levels)) is not None
    with pytest.raises(BuilderValidationError):
        parse_filter(json.dumps(four_levels))
    assert len(parse_filter(json.dumps(group(*(term(i) for i in range(50))))).children) == 50
    with pytest.raises(BuilderValidationError):
        parse_filter(json.dumps(group(*(term(i) for i in range(51)))))


def test_component_query_uses_same_node_budget() -> None:
    values = ",".join(f"field_{i}.eq.{i}" for i in range(49))
    assert len(parse_terms([("and", f"({values})")])[1].children) == 1
    values = ",".join(f"field_{i}.eq.{i}" for i in range(50))
    with pytest.raises(BuilderValidationError):
        parse_terms([("and", f"({values})")])


def test_compiler_prunes_empty_terms_and_handles_negated_groups() -> None:
    tree = FilterGroup(
        "not.or",
        (FilterTerm("a", "is", 1), FilterTerm("b", "is", 2)),
    )
    clean, expression, count = compile_filter(
        tree, lambda node: sa.literal(node.value == 1) if node.field == "a" else None
    )
    assert clean == FilterGroup("not.or", (FilterTerm("a", "is", 1),))
    assert expression is not None
    assert count == 1
