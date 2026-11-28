from __future__ import annotations

import json
from typing import Any

import pytest
import sqlalchemy as sa
from obox import ModelResource
from obox.core.exceptions import InvalidQuery
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
    resource = ModelResource(model=FilterRow)
    state = resource.collection({"filter": json.dumps(data)})
    assert state.filter["children"] == [{"field": "name", "op": "contains", "value": "Ada"}]
    assert state.predicate is not None
    query = state.apply(sa.select(FilterRow), FilterRow, ())
    assert "filter_query_rows.name" in str(query.compile(dialect=sqlite.dialect()))


def test_db_filters_reject_depth_four_and_more_than_fifty_nodes() -> None:
    resource = ModelResource(model=FilterRow)
    child: dict[str, Any] = {"field": "name", "op": "is", "value": "Ada"}
    for _ in range(4):
        child = {"conjunction": "and", "children": [child]}
    with pytest.raises(InvalidQuery):
        resource.collection({"filter": json.dumps(child)})

    too_many = {
        "conjunction": "and",
        "children": [{"field": "name", "op": "is", "value": str(index)} for index in range(51)],
    }
    with pytest.raises(InvalidQuery):
        resource.collection({"filter": json.dumps(too_many)})
