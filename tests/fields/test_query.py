from __future__ import annotations

from urllib.parse import unquote_plus

import pytest
import sqlalchemy as sa
from obox_builder.fields import FieldSpec, QuerySpec, QuerySpecError, field_types

name_column = sa.column("name", sa.String)
age_column = sa.column("age", sa.Integer)
flag_column = sa.column("flag", sa.Boolean)

SPECS = {
    "name": FieldSpec(
        name="name", label="Name", type=field_types.create("text"), column=name_column
    ),
    "age": FieldSpec(
        name="age", label="Age", type=field_types.create("integer"), column=age_column
    ),
    "flag": FieldSpec(
        name="flag",
        label="Flag",
        type=field_types.create("checkbox"),
        column=flag_column,
        nullable=False,
    ),
}


def parse(query: str) -> QuerySpec:
    return QuerySpec.parse(_Params(query), SPECS, default_sort=("-age",))


class _Params:
    """Minimal stand-in for Starlette's QueryParams."""

    def __init__(self, query: str) -> None:
        self._multi: dict[str, list[str]] = {}
        for pair in query.split("&"):
            if not pair:
                continue
            key, _, value = pair.partition("=")
            self._multi.setdefault(unquote_plus(key), []).append(unquote_plus(value))

    def get(self, key: str) -> str | None:
        values = self._multi.get(key)
        return values[-1] if values else None

    def getlist(self, key: str) -> list[str]:
        return list(self._multi.get(key, []))


def test_defaults_come_from_admin_ordering() -> None:
    spec = parse("")
    assert spec.page == 1
    assert spec.size == 50
    assert spec.q == ""
    assert [term.field for term in spec.sort] == ["age"]
    assert spec.sort[0].descending is True


def test_parses_filters_sort_and_search() -> None:
    spec = parse("q=widget&f=name:contains:wid&f=age:gte:18&sort=-name,age&page=3&size=10")
    assert spec.q == "widget"
    assert [(term.field, term.op) for term in spec.filters] == [
        ("name", "contains"),
        ("age", "gte"),
    ]
    assert spec.filters[1].value == "18"
    assert [(term.field, term.descending) for term in spec.sort] == [
        ("name", True),
        ("age", False),
    ]
    assert (spec.page, spec.size) == (3, 10)


def test_filter_without_operator_defaults_to_eq() -> None:
    spec = parse("f=name:draft")
    assert spec.filters[0].op == "eq"
    assert spec.filters[0].value == "draft"


def test_isnull_and_in_filters() -> None:
    spec = parse("f=age:isnull:true&f=name:in:a,b,c")
    assert spec.filters[0].value is True
    assert spec.filters[1].value == ["a", "b", "c"]


def test_size_is_clamped() -> None:
    assert parse("size=0").size == 1
    assert parse("size=9999").size == 200


def test_page_is_clamped_to_one() -> None:
    assert parse("page=0").page == 1
    with pytest.raises(QuerySpecError, match="page"):
        parse("page=abc")


def test_unknown_filter_field_rejected() -> None:
    with pytest.raises(QuerySpecError, match="Unknown filter field"):
        parse("f=nope:eq:1")


def test_operator_not_allowed_for_type_rejected() -> None:
    with pytest.raises(QuerySpecError, match="not allowed"):
        parse("f=flag:contains:1")


def test_empty_eq_value_rejected() -> None:
    with pytest.raises(QuerySpecError, match="value is required"):
        parse("f=name:eq:")


def test_bad_isnull_value_rejected() -> None:
    with pytest.raises(QuerySpecError, match="isnull"):
        parse("f=age:isnull:maybe")


def test_unknown_sort_field_rejected() -> None:
    with pytest.raises(QuerySpecError, match="Unknown sort field"):
        parse("sort=nope")


def test_round_trip_query_string() -> None:
    spec = parse("q=wid&f=name:contains:wid&sort=-name&page=2&size=10")
    assert spec.to_query_string() == "q=wid&f=name%3Acontains%3Awid&sort=-name&page=2&size=10"
    again = QuerySpec.parse(_Params(spec.to_query_string()), SPECS)
    assert again.q == spec.q
    assert (again.page, again.size) == (spec.page, spec.size)
    assert [(term.field, term.op, term.value) for term in again.filters] == [
        (term.field, term.op, term.value) for term in spec.filters
    ]


def test_apply_filters_sort_and_search_against_sqlite() -> None:
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    table = sa.Table(
        "things",
        metadata,
        sa.Column("name", sa.String),
        sa.Column("age", sa.Integer),
        sa.Column("flag", sa.Boolean),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            table.insert(),
            [
                {"name": "Widget", "age": 30, "flag": True},
                {"name": "Wodget", "age": 20, "flag": True},
                {"name": "Gadget", "age": 40, "flag": False},
            ],
        )
    specs = {
        "name": FieldSpec(
            name="name", label="Name", type=field_types.create("text"), column=table.c.name
        ),
        "age": FieldSpec(
            name="age", label="Age", type=field_types.create("integer"), column=table.c.age
        ),
        "flag": FieldSpec(
            name="flag",
            label="Flag",
            type=field_types.create("checkbox"),
            column=table.c.flag,
            nullable=False,
        ),
    }
    spec = QuerySpec.parse(_Params("q=w&f=age:gte:25&sort=-age"), specs)
    stmt = spec.apply(sa.select(table.c.name), specs, search_specs=[specs["name"]])
    with engine.begin() as connection:
        rows = connection.execute(stmt).all()
    assert [row.name for row in rows] == ["Widget"]


def test_apply_rejects_filter_on_columnless_spec() -> None:
    from obox_builder.fields import FilterTerm

    specs = dict(SPECS)
    specs["ghost"] = FieldSpec(name="ghost", label="Ghost", type=field_types.create("text"))
    with pytest.raises(QuerySpecError, match="cannot be filtered"):
        QuerySpec.parse(_Params("f=ghost:eq:x"), specs)
    spec = QuerySpec(filters=[FilterTerm(field="ghost", op="eq", value="x")])
    with pytest.raises(QuerySpecError, match="cannot be filtered"):
        spec.apply(sa.select(sa.literal(1)), specs)
