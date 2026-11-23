from __future__ import annotations

from decimal import Decimal, localcontext
from typing import Any, cast

import pytest
from obox.contrib.analytics import Avg, Count, CountNumbers, Max, Min, Pivot, Sum
from obox.core.exceptions import BuilderValidationError, ImproperlyConfigured

SALES = [
    {"category": "fpt", "code": "FPT001", "status": "OK", "channel": "web", "amount": 10},
    {"category": "fpt", "code": "FPT002", "status": "OK", "channel": "store", "amount": 20},
    {"category": "fpt", "code": "FPT003", "status": "NG", "channel": "web", "amount": 30},
    {"category": "viettel", "code": "VT001", "status": "OK", "channel": "web", "amount": 40},
    {"category": "viettel", "code": "VT002", "status": "NG", "channel": "store", "amount": 50},
    {"category": "viettel", "code": "VT003", "status": "OK", "channel": "store", "amount": 60},
    {"category": "viettel", "code": "VT004", "status": "NG", "channel": "web", "amount": 70},
    {"category": "viettel", "code": "VT005", "status": "NG", "channel": "store", "amount": 80},
    {"category": "vnpt", "code": "VNPT001", "status": "OK", "channel": "store", "amount": 90},
    {"category": "vnpt", "code": "VNPT002", "status": "NG", "channel": "web", "amount": 100},
]
MEASURES = (Count("code"), Sum("amount"), Avg("amount"))


def matrix(*, data: Any = SALES, **options: Any) -> Any:
    return Pivot(
        data=data,
        rows=("category", "code"),
        columns=("status", "channel"),
        values=MEASURES,
        **options,
    ).compute()


def path_index(paths: list[list[tuple[str, Any]]], *values: Any) -> int:
    path = [(type(value).__name__, value) for value in values]
    return paths.index(path)


def test_multiple_row_column_fields_and_measures_match_pivot_totals() -> None:
    result = matrix()
    fpt = path_index(result.rows, "fpt")
    fpt001 = path_index(result.rows, "fpt", "FPT001")
    ng = path_index(result.columns, "NG")
    ng_store = path_index(result.columns, "NG", "store")
    ok = path_index(result.columns, "OK")

    assert result.row_fields == ["category", "code"]
    assert result.column_fields == ["status", "channel"]
    assert result.values == ["Count of code", "Sum of amount", "Average of amount"]
    assert result.records == 10
    assert result.cells[fpt][ng] == [1, 30, "30"]
    assert result.cells[fpt][ok] == [2, 30, "15"]
    assert result.row_totals[fpt] == [3, 60, "20"]
    assert result.cells[fpt001][ng_store] == [None, None, None]
    assert result.cells[fpt001][path_index(result.columns, "OK", "web")] == [1, 10, "10"]
    assert result.column_totals[ng] == [5, 330, "66"]
    assert result.total == [10, 550, "55"]


@pytest.mark.parametrize(
    ("values_axis", "values_position"),
    [("rows", 0), ("rows", 1), ("rows", 2), ("columns", 0), ("columns", 1), ("columns", 2)],
)
def test_value_field_axis_and_insertion_position_preserve_matrix(
    values_axis: str, values_position: int
) -> None:
    result = matrix(values_axis=values_axis, values_position=values_position)

    assert result.values_axis == values_axis
    assert result.values_position == values_position
    assert result.total == [10, 550, "55"]
    assert result.cells[path_index(result.rows, "fpt")][path_index(result.columns, "NG")] == [
        1,
        30,
        "30",
    ]


def test_memory_aggregates_count_nulls_text_booleans_and_zero_correctly() -> None:
    data = [
        {"group": "A", "value": 10, "label": "x", "flag": True},
        {"group": "A", "value": None, "label": None, "flag": False},
        {"group": "A", "value": "7", "label": "", "flag": True},
        {"group": "B", "value": -2, "label": "z", "flag": False},
        {"group": "B", "value": 0, "label": "q", "flag": True},
    ]
    result = Pivot(
        data=data,
        rows="group",
        values=(
            Count(),
            Count("label"),
            CountNumbers("value"),
            CountNumbers("flag"),
            Sum("value"),
            Avg("value"),
            Min("value"),
            Max("value"),
        ),
    ).compute()

    assert result.total[:4] == [5, 4, 3, 0]
    assert result.total[4] == "8"
    with localcontext() as context:
        context.prec = 38
        assert Decimal(cast(str, result.total[5])) == Decimal(8) / Decimal(3)
    assert result.total[6:] == [-2, 10]
    assert result.row_totals == [
        [3, 2, 1, 0, "10", "10", 10, 10],
        [2, 2, 2, 0, -2, "-1", -2, 0],
    ]


def test_empty_intersection_is_distinct_from_zero_and_all_null_values() -> None:
    result = Pivot(
        data=[
            {"group": "all-null", "column": "x", "value": None},
            {"group": "zero", "column": "x", "value": 0},
        ],
        rows="group",
        columns="column",
        values=(Sum("value"), Count("value")),
    ).compute()

    all_null = path_index(result.rows, "all-null")
    zero = path_index(result.rows, "zero")
    x = path_index(result.columns, "x")
    assert result.cells[all_null][x] == [None, None]
    assert result.cells[zero][x] == [0, 1]
    assert result.total == [0, 1]


def test_input_is_snapshotted_and_one_shot_iterables_are_supported() -> None:
    source = [{"group": "before", "value": 2}]
    pivot = Pivot(data=source, rows="group", values=(Sum("value"),))
    source[0]["group"] = "after"
    source.append({"group": "after", "value": 3})

    assert pivot.compute().rows == [[("str", "before")]]
    assert Pivot(rows="group", values=(Sum("value"),)).compute(iter(source)).records == 2


def test_typed_group_keys_remain_distinct_and_null_sorts_last() -> None:
    result = Pivot(
        data=[
            {"key": 1},
            {"key": True},
            {"key": "1"},
            {"key": ""},
            {"key": None},
        ],
        rows="key",
        values=(Count(),),
    ).compute()

    assert {tuple(path[0]) for path in result.rows} == {
        ("number", "1"),
        ("bool", True),
        ("str", "1"),
        ("str", ""),
        ("NoneType", None),
    }
    assert result.rows[-1] == [("NoneType", None)]
    assert result.total == [5]


def test_empty_source_returns_an_empty_matrix_with_no_partial_groups() -> None:
    result = Pivot(fields=("group",), rows="group", values=(Count(),)).compute([])

    assert result.records == 0
    assert result.rows == []
    assert result.columns == []
    assert result.cells == []
    assert result.total == [None]


@pytest.mark.parametrize("rows", [42, ("",), ("group", 3)])
def test_axes_require_ordered_nonempty_field_names(rows: Any) -> None:
    with pytest.raises(ImproperlyConfigured):
        Pivot(data=[{"group": "A"}], rows=rows).compute()


def test_non_finite_group_keys_are_rejected() -> None:
    with pytest.raises(BuilderValidationError):
        Pivot(data=[{"key": float("inf")}], rows="key").compute()


def test_compute_requires_an_explicit_data_source() -> None:
    with pytest.raises(ImproperlyConfigured):
        Pivot(rows="group").compute()


@pytest.mark.parametrize("module_name", ["pandas", "polars"])
def test_compute_accepts_optional_dataframe_adapters(module_name: str) -> None:
    module = pytest.importorskip(module_name)
    frame = module.DataFrame(
        [
            {"group": "A", "value": 4.0},
            {"group": "A", "value": float("nan")},
            {"group": "B", "value": 0.0},
        ]
    )

    values = (Count(), Sum("value"))
    result = Pivot(rows="group", values=values).compute(frame)
    configured = Pivot(data=frame, rows="group", values=values).compute()

    assert result.records == 3
    assert result.total == [3, "4.0"]
    assert result.row_totals == [[2, "4.0"], [1, "0.0"]]
    assert configured.model_dump() == result.model_dump()


def test_compute_applies_fixed_filters_and_rejects_fields_outside_filter_policy() -> None:
    pivot = Pivot(
        data=SALES,
        rows="category",
        values=(Count(),),
        filters={"status": "OK"},
        filter_fields=("category",),
    )

    with pytest.raises(BuilderValidationError):
        pivot.compute()

    filtered = Pivot(
        data=SALES,
        rows="category",
        values=(Count(),),
        filters={"status": "OK"},
        filter_fields=("status",),
    ).compute()
    assert filtered.records == 5
    assert filtered.total == [5]


@pytest.mark.parametrize(
    "configure",
    [
        lambda: Pivot(data=[], source=object),
        lambda: Pivot(data=[{"a": 1}], rows=("a", "a")),
        lambda: Pivot(data=[{"a": 1}], rows="a", values=[]),
        lambda: Pivot(data=[{"a": 1}], rows="a", values=(Count("missing"),)),
        lambda: Pivot(data=[{"a": 1}], rows="a", values=(Sum("a"),), values_position=1),
        lambda: Pivot(data=[{"a": [1]}], rows="a"),
    ],
)
def test_invalid_memory_pivot_declarations_are_rejected(configure: Any) -> None:
    with pytest.raises(ImproperlyConfigured):
        configure().compute()


@pytest.mark.parametrize(
    "limits",
    [
        {"max_rows": 1},
        {"max_columns": 1},
        {"max_cells": 2},
        {"max_rows": 0},
        {"max_columns": True},
    ],
)
def test_compute_enforces_matrix_limits_without_returning_partial_results(
    limits: dict[str, Any],
) -> None:
    with pytest.raises((BuilderValidationError, ImproperlyConfigured)):
        Pivot(
            data=SALES,
            rows=("category", "code"),
            columns=("status", "channel"),
            values=MEASURES,
        ).compute(**limits)


def test_value_sort_is_stable_with_nulls_and_nested_groups() -> None:
    result = Pivot(
        data=[
            {"parent": "A", "child": "low", "value": 1},
            {"parent": "A", "child": "high", "value": 9},
            {"parent": "B", "child": "other", "value": 4},
            {"parent": "C", "child": "empty", "value": None},
        ],
        rows=("parent", "child"),
        values=(Sum("value"),),
        row_sort="value",
        row_descending=True,
    ).compute()

    assert result.rows[0] == [("str", "A")]
    assert result.rows[1] == [("str", "A"), ("str", "high")]
    assert result.rows[-2:] == [[("str", "C")], [("str", "C"), ("str", "empty")]]
