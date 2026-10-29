from __future__ import annotations

from urllib.parse import unquote_plus

import pytest
import sqlalchemy as sa
from obox_builder.fields import FieldSpec, QuerySpec, QuerySpecError, field_types
from sqlalchemy import event

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
    assert spec.filters[1].value == 18
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
    from obox_builder.db.search import fold_text

    event.listen(
        engine,
        "connect",
        lambda connection, _: connection.create_function("obox_search_text", 1, fold_text),
    )
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


def test_filter_tree_round_trips_typed_values() -> None:
    import datetime as dt
    import json

    specs = dict(
        SPECS,
        day=FieldSpec(
            name="day",
            label="Day",
            type=field_types.create("date"),
            column=sa.column("day", sa.Date),
        ),
    )
    tree = {
        "conjunction": "or",
        "children": [
            {"field": "age", "op": "in", "value": [18, "30"]},
            {
                "conjunction": "and",
                "children": [
                    {"field": "day", "op": "gte", "value": "2026-10-23"},
                    {"field": "flag", "op": "eq", "value": False},
                ],
            },
        ],
    }
    query = QuerySpec.parse({"filter": json.dumps(tree)}, specs)
    assert query.filter_group is not None
    assert query.filter_group.children[0].value == [18, 30]  # type: ignore[union-attr]
    again = QuerySpec.parse(_Params(query.to_query_string()), specs)
    assert again.filter_group == query.filter_group
    assert again.filter_group.children[1].children[0].value == dt.date(2026, 10, 23)  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "params",
    [
        {"f": "age:gt:bad"},
        {"f": "age:gt:"},
        {"f": "flag:eq:maybe"},
        {"f": "age:in:1,,2"},
        {"sort": "age,-age"},
        {"group": "age,age"},
        {"group": "age,flag,name,age"},
        {"sort": "age,flag,name,age,flag,name"},
        {"filter": "not json"},
        {"filter": "[]"},
        {"filter": '{"conjunction":"xor","children":[]}'},
        {"filter": '{"conjunction":"and","children":[{}]}'},
        {"filter": '{"conjunction":"and","children":[{"field":[],"op":"eq","value":1}]}'},
        {"q": "x" * 201},
        {"f": ["age:eq:1"] * 101},
    ],
)
def test_invalid_queries_fail_before_execution(params: dict) -> None:
    with pytest.raises(QuerySpecError):
        QuerySpec.parse(params, SPECS)


def test_filter_tree_depth_and_node_limits() -> None:
    import json

    tree = {"conjunction": "and", "children": [{"field": "age", "op": "gt", "value": 1}]}
    for _ in range(2):
        tree = {"conjunction": "or", "children": [tree]}
    assert QuerySpec.parse({"filter": json.dumps(tree)}, SPECS).filter_group is not None
    tree = {"conjunction": "and", "children": [tree]}
    with pytest.raises(QuerySpecError, match="levels"):
        QuerySpec.parse({"filter": json.dumps(tree)}, SPECS)
    tree = {"conjunction": "and", "children": [{"field": "age", "op": "gt", "value": 1}] * 101}
    with pytest.raises(QuerySpecError, match="100"):
        QuerySpec.parse({"filter": json.dumps(tree)}, SPECS)


@pytest.mark.parametrize("parameter", ["f", "sort", "group"])
def test_secret_fields_cannot_be_queried(parameter: str) -> None:
    from dataclasses import replace

    specs = {"name": replace(SPECS["name"], options={"secret": True})}
    with pytest.raises(QuerySpecError):
        QuerySpec.parse({parameter: "name:eq:private" if parameter == "f" else "name"}, specs)


def test_nulls_last_stable_sort_and_group_precedence() -> None:
    from obox_builder.fields import SortTerm

    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    table = sa.Table(
        "items",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("age", sa.Integer),
        sa.Column("name", sa.String),
    )
    metadata.create_all(engine)
    specs = {
        **SPECS,
        "id": FieldSpec(
            name="id",
            label="ID",
            type=field_types.create("integer"),
            column=table.c.id,
            primary_key=True,
        ),
        "age": FieldSpec(
            name="age", label="Age", type=field_types.create("integer"), column=table.c.age
        ),
        "name": FieldSpec(
            name="name", label="Name", type=field_types.create("text"), column=table.c.name
        ),
    }
    with engine.begin() as connection:
        connection.execute(
            table.insert(),
            [
                {"id": 3, "age": 20, "name": "Same"},
                {"id": 1, "age": None, "name": "A"},
                {"id": 2, "age": 20, "name": "Same"},
                {"id": 4, "age": 10, "name": "Z"},
            ],
        )
        query = QuerySpec(sort=[SortTerm("name")], group=[SortTerm("age")])
        stmt = query.apply(sa.select(table.c.id).order_by(table.c.id.desc()), specs)
        assert connection.scalars(stmt).all() == [4, 3, 2, 1]
        assert connection.scalars(stmt.limit(1).offset(1)).all() == [3]
    engine.dispose()


def test_empty_subgroups_do_not_bypass_or_conditions() -> None:
    import json

    tree = {
        "conjunction": "or",
        "children": [
            {"field": "age", "op": "gte", "value": 18},
            {"conjunction": "and", "children": []},
        ],
    }
    query = QuerySpec.parse({"filter": json.dumps(tree)}, SPECS)
    assert query.filter_group is not None
    assert len(query.filter_group.children) == 1
    combined = QuerySpec.parse({"f": "flag:eq:true", "filter": json.dumps(tree)}, SPECS)
    assert combined.combined_filters().conjunction == "and"
    assert (
        QuerySpec.parse(
            {"filter": json.dumps(combined.combined_filters().to_dict())}, SPECS
        ).filter_group
        == combined.combined_filters()
    )


def test_decimal_nonfinite_and_invalid_date_filters_rejected() -> None:
    specs = {
        "price": FieldSpec(
            name="price",
            label="Price",
            type=field_types.create("decimal"),
            column=sa.column("price", sa.Numeric),
        ),
        "day": FieldSpec(
            name="day",
            label="Day",
            type=field_types.create("date"),
            column=sa.column("day", sa.Date),
        ),
    }
    for raw in ["price:eq:NaN", "price:eq:Infinity", "day:eq:2026-02-30"]:
        with pytest.raises(QuerySpecError):
            QuerySpec.parse({"f": raw}, specs)


@pytest.mark.parametrize("kind,value", [("email", "example"), ("url", "example"), ("phone", "090")])
def test_partial_text_filters_do_not_require_complete_form_values(kind: str, value: str) -> None:
    specs = {
        "contact": FieldSpec(
            name="contact",
            label="Contact",
            type=field_types.create(kind),
            column=sa.column("contact", sa.String),
        )
    }
    query = QuerySpec.parse({"f": f"contact:contains:{value}"}, specs)
    assert query.filters[0].value == value
