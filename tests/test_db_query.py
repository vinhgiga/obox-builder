from __future__ import annotations

import json
from typing import Any

import pytest
import sqlalchemy as sa
from obox.core.exceptions import InvalidQuery
from obox.db.query import filter_expression, list_query
from sqlalchemy.dialects import sqlite
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class FilterRow(Base):
    __tablename__ = "filter_query_rows"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(40))


def test_db_filters_prune_empty_terms_and_compile_active_conditions() -> None:
    data: dict[str, Any] = {
        "conjunction": "and",
        "children": [
            {"field": "name", "op": "contains", "value": ""},
            {"field": "name", "op": "contains", "value": "Ada"},
        ],
    }
    cleaned, expression, count = filter_expression(FilterRow, data, {"name"})
    assert cleaned["children"] == [{"field": "name", "op": "contains", "value": "Ada"}]
    assert expression is not None
    assert count == 1

    query = list_query(
        sa.select(FilterRow),
        FilterRow,
        {"filter": json.dumps(data)},
        filters=(),
        allowed={"name"},
    )
    assert "filter_query_rows.name" in str(query.compile(dialect=sqlite.dialect()))


def test_db_filters_reject_depth_four_and_more_than_fifty_nodes() -> None:
    child: dict[str, Any] = {"field": "name", "op": "is", "value": "Ada"}
    for _ in range(4):
        child = {"conjunction": "and", "children": [child]}
    with pytest.raises(InvalidQuery):
        filter_expression(FilterRow, child, {"name"})

    too_many = {
        "conjunction": "and",
        "children": [{"field": "name", "op": "is", "value": str(index)} for index in range(51)],
    }
    with pytest.raises(InvalidQuery):
        filter_expression(FilterRow, too_many, {"name"})
