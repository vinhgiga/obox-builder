"""Bounded chart projections over the resource service's authorized selection."""

from __future__ import annotations

import base64
import hmac
import json
import time
from collections import defaultdict
from decimal import Decimal, localcontext
from enum import Enum
from typing import Any

import sqlalchemy as sa
from pydantic import TypeAdapter, ValidationError

from obox_builder.builder.aggregates import State, encode, exceeded
from obox_builder.builder.service import _filter_tree, invoke
from obox_builder.contrib.analytics.charts import Chart, Field
from obox_builder.errors import (
    BuilderValidationError,
    ImproperlyConfigured,
    NotFound,
    PermissionDenied,
)


async def statement(service: Any, chart: Chart, ctx: Any, **options: Any) -> tuple[Any, set[str]]:
    await service.authorize("list", ctx)
    selected, readable = await service._list_statement(ctx=ctx, **options)
    if not set(chart.fields).issubset(
        readable.intersection(service.builder.policy.aggregate_fields(ctx))
    ):
        raise PermissionDenied("These fields are not available for charting.")
    narrow = chart.statement
    if narrow is None and chart._definition is not None:
        narrow = chart._definition().get_statement
    if narrow is not None:
        extra = await invoke(narrow, ctx)
        service._context(ctx)
        if extra is not None:
            if not isinstance(extra, sa.Select):
                raise ImproperlyConfigured("Chart statement must return a Select or None.")
            ids = extra.with_only_columns(service.builder.pk, maintain_column_froms=True).order_by(
                None
            )
            selected = selected.where(service.builder.pk.in_(ids))
    for dimension in (chart.dimension, chart.group):
        if dimension is not None and not dimension.include_empty:
            column = getattr(service.builder.model, dimension.field)
            selected = selected.where(column.is_not(None))
            if isinstance(service.builder.specs[dimension.field].column.type, sa.String):
                selected = selected.where(column != "")
    return selected, readable


def identity(value: Any) -> str:
    return json.dumps(encode(value), sort_keys=True, separators=(",", ":"))


async def execute(
    service: Any,
    chart: Chart,
    ctx: Any,
    *,
    max_rows: int,
    max_groups: int,
    max_cells: int,
    max_points: int,
    **options: Any,
) -> dict[str, Any]:
    selected, readable = await statement(service, chart, ctx, **options)
    builder = service.builder
    orders = tuple(builder.ordering or ())
    if chart.order != "source":
        orders = ()
    elif any(
        name.removeprefix("-") not in readable and name.removeprefix("-") != builder.pk_name
        for name in orders
    ):
        raise PermissionDenied("The source ordering is not available.")
    names = tuple(
        dict.fromkeys(
            (builder.pk_name, *chart.fields, *[name.removeprefix("-") for name in orders])
        )
    )
    masks = []
    for index, series in enumerate(chart.series):
        if series.where and series.where.children:
            condition = _filter_tree(
                builder, readable, series.where, ctx.session.get_bind().dialect.name
            )
            if condition is None:
                condition = sa.true()
            masks.append(sa.case((condition, 1)).label(f"_series_{index}"))
    scope = (
        selected.order_by(None)
        .with_only_columns(
            *[getattr(builder.model, name).label(name) for name in names],
            *masks,
            maintain_column_froms=True,
        )
        .distinct()
        .subquery()
    )
    ordering = [
        scope.c[name.removeprefix("-")].desc() if name.startswith("-") else scope.c[name].asc()
        for name in orders
    ]
    scope = sa.select(
        scope,
        sa.func.row_number()
        .over(
            order_by=[*ordering, scope.c[builder.pk_name]],
        )
        .label("_position"),
    ).subquery()
    dimensions = [scope.c[chart.dimension.field]]
    if chart.group:
        dimensions.append(scope.c[chart.group.field])
    values: list[Any] = []
    for index, series in enumerate(chart.series):
        value = series.value
        name = value.name if isinstance(value, Field) else value.field
        column: Any = scope.c[name] if name else None
        if chart.raw:
            values.append(column)
        else:
            operation = series.measure.operation
            matched = sa.func.count()
            if series.where and series.where.children:
                mask = scope.c[f"_series_{index}"]
                matched = sa.func.count(mask)
                column = sa.case((mask.is_not(None), column if column is not None else 1))
            values.extend(
                [
                    sa.func.count()
                    if column is None
                    else getattr(
                        sa.func,
                        "sum" if operation == "avg" else operation,
                    )(column),
                    sa.func.count() if column is None else sa.func.count(column),
                    matched,
                ]
            )
    bound = min(max_points if chart.raw else max_cells, max_cells) // len(chart.series)
    if bound < 1:
        raise exceeded()
    query = (
        sa.select(*dimensions, scope.c[builder.pk_name], scope.c._position, *values)
        if chart.raw
        else (
            sa.select(
                *dimensions, sa.func.count(), sa.func.min(scope.c._position), *values
            ).group_by(*dimensions)
        )
    )
    records = (await ctx.session.execute(query.limit(bound + 1))).all()
    if len(records) > bound:
        raise exceeded()
    entries = []
    categories: dict[str, Any] = {}
    groups: dict[str, Any] = {}
    ranks: dict[str, int] = {}
    totals: dict[str, list[State]] = {}
    count = 0
    offset = len(dimensions)
    for record in records:
        x, group = record[0], record[1] if chart.group else None
        key = identity(x)
        categories[key], groups[identity(group)] = x, group
        ranks[key] = min(ranks.get(key, record[offset + 1]), record[offset + 1])
        states = [
            State(1, int(record[offset + 2 + i] is not None), record[offset + 2 + i])
            if chart.raw
            else State(
                record[offset + 4 + 3 * i], record[offset + 3 + 3 * i], record[offset + 2 + 3 * i]
            )
            for i in range(len(chart.series))
        ]
        count += 1 if chart.raw else record[offset]
        if not chart.raw:
            category_totals = totals.setdefault(key, [State() for _ in chart.series])
            with localcontext() as context:
                context.prec = 38
                for i, series in enumerate(chart.series):
                    category_totals[i].merge(states[i], series.measure.operation)
        entries.append(
            (x, group, record[offset] if chart.raw else None, record[offset + 1], states)
        )
    if (
        len(categories) > max_rows
        or len(groups) > max_groups
        or (not chart.raw and len(categories) * len(groups) * len(chart.series) > max_cells)
    ):
        raise exceeded()
    target = chart.order.removeprefix("-")
    sort_index = next(
        (i for i, series in enumerate(chart.series) if series.id == target.removeprefix("y:")), 0
    )
    if target == "y":
        sort_index = next(i for i, series in enumerate(chart.series) if series.axis == "primary")

    def sort_value(entry: Any) -> Any:
        if target == "source":
            return entry[3] if chart.raw else ranks[identity(entry[0])]
        if target.startswith("y"):
            series = chart.series[sort_index]
            state = entry[4][sort_index] if chart.raw else totals[identity(entry[0])][sort_index]
            return state.value if chart.raw else state.result(series.measure.operation)
        return entry[0].value if isinstance(entry[0], Enum) else entry[0]

    entries.sort(key=lambda entry: entry[3])
    if target.startswith("y"):
        entries.sort(
            key=lambda entry: (
                entry[0] is None,
                entry[0].value if isinstance(entry[0], Enum) else entry[0],
            )
        )
    available = [entry for entry in entries if sort_value(entry) is not None]
    missing = [entry for entry in entries if sort_value(entry) is None]
    available.sort(key=sort_value, reverse=chart.order.startswith("-"))
    entries = available + missing
    category_order = list(dict.fromkeys(identity(entry[0]) for entry in entries))
    group_order = sorted(
        groups,
        key=lambda key: (
            groups[key] is None,
            groups[key].value if isinstance(groups[key], Enum) else groups[key],
        ),
    )
    datasets = []
    stack_totals: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    for series_index, series in enumerate(chart.series):
        for group_key in group_order:
            points = []
            for x, group, pk, _, states in entries:
                if identity(group) != group_key:
                    continue
                state = states[series_index]
                value = state.value if chart.raw else state.result(series.measure.operation)
                if chart.kind in {"pie", "donut"} and value is not None and value < 0:
                    raise BuilderValidationError(
                        {"chart": ["Negative values require a bar chart."]}, code="negative_chart"
                    )
                if chart.stack == "percent" and value is not None:
                    if value < 0:
                        raise BuilderValidationError(
                            {"chart": ["Percent stacks require nonnegative values."]}
                        )
                    with localcontext() as context:
                        context.prec = 38
                        stack_totals[identity(x), series.axis] += Decimal(str(value))
                points.append(
                    {
                        "x": encode(x),
                        "y": encode(value),
                        "recordCount": state.records,
                        "state": "noFacts"
                        if not state.records
                        else "allNull"
                        if value is None
                        else "value",
                        "key": {"record": encode(pk), "series": series.id}
                        if chart.raw
                        else {"x": encode(x), "group": encode(group), "series": series.id},
                    }
                )
            if not chart.raw:
                existing = {identity(point["x"]): point for point in points}
                points = [
                    existing.get(
                        key,
                        {
                            "x": encode(categories[key]),
                            "y": 0 if series.measure.operation == "count" else None,
                            "recordCount": 0,
                            "state": "noFacts",
                            "key": {
                                "x": encode(categories[key]),
                                "group": encode(groups[group_key]),
                                "series": series.id,
                            },
                        },
                    )
                    for key in category_order
                ]
            label = series.label or str(series.id).replace("_", " ").title()
            if chart.group:
                label += " · " + ("∅" if groups[group_key] is None else str(groups[group_key]))
            datasets.append(
                {
                    "id": series.id,
                    "group": encode(groups[group_key]),
                    "label": label,
                    "axis": series.axis,
                    "kind": series.kind or chart.kind,
                    "valueType": "integer"
                    if not chart.raw and series.measure.operation == "count"
                    else "number",
                    "points": points,
                }
            )
    if chart.stack == "percent":
        with localcontext() as context:
            context.prec = 38
            for dataset in datasets:
                for point in dataset["points"]:
                    denominator = stack_totals[identity(point["x"]), dataset["axis"]]
                    point["plotY"] = (
                        str(Decimal(str(point["y"])) / denominator)
                        if denominator and point["y"] is not None
                        else None
                    )
    if chart.percent:
        with localcontext() as context:
            context.prec = 38
            points = datasets[0]["points"] if datasets else []
            denominator = sum(
                (Decimal(str(point["y"])) for point in points if point["y"] is not None), Decimal()
            )
            for point in points:
                point["percentage"] = (
                    str(Decimal(str(point["y"])) / denominator)
                    if denominator and point["y"] is not None
                    else None
                )
    return {
        "mode": "raw" if chart.raw else "aggregate",
        "records": count,
        "series": datasets,
        "categories": [encode(categories[key]) for key in category_order],
        "grouped": bool(chart.group),
        "xLabel": chart.dimension.label or chart.dimension.field.replace("_", " ").title(),
        "xType": "number"
        if isinstance(builder.specs[chart.dimension.field].column.type, (sa.Integer, sa.Numeric))
        else "time"
        if isinstance(builder.specs[chart.dimension.field].column.type, (sa.Date, sa.DateTime))
        else "category",
        "pointCount": sum(len(dataset["points"]) for dataset in datasets),
    }


def selector(
    secret: bytes, binding: str, *, key: dict[str, Any] | None = None, token: str | None = None
) -> Any:
    secret = hmac.digest(secret, b"obox.chart.selection" + binding.encode(), "sha256")
    if key is not None:
        payload = base64.urlsafe_b64encode(
            json.dumps([int(time.time()), key], separators=(",", ":")).encode()
        )
        if len(payload) + 65 > 32_000:
            raise BuilderValidationError({"selector": ["Chart selection is too large."]})
        return (payload + b"." + hmac.digest(secret, payload, "sha256").hex().encode()).decode()
    try:
        if not token or len(token) > 32_000:
            raise ValueError
        payload, signature = token.encode("ascii").split(b".")
        if not hmac.compare_digest(
            signature, hmac.digest(secret, payload, "sha256").hex().encode()
        ):
            raise ValueError
        timestamp, value = json.loads(base64.urlsafe_b64decode(payload))
        if not 0 <= time.time() - timestamp <= 3600 or not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise BuilderValidationError({"selector": ["Invalid or expired chart selection."]}) from exc


async def records(
    service: Any,
    chart: Chart,
    ctx: Any,
    key: dict[str, Any],
    *,
    offset: int,
    limit: int,
    **options: Any,
) -> Any:
    if not chart.drill:
        raise NotFound("Record inspection is disabled.")
    if offset < 0 or not 1 <= limit <= 200:
        raise BuilderValidationError({"pagination": ["Use offset >= 0 and limit from 1 to 200."]})
    selected, readable = await statement(service, chart, ctx, **options)
    builder = service.builder
    series = next((series for series in chart.series if series.id == key.get("series")), None)
    if series is None:
        raise BuilderValidationError({"selector": ["Unknown chart series."]})
    if series.where is not None:
        condition = _filter_tree(
            builder, readable, series.where, ctx.session.get_bind().dialect.name
        )
        if condition is not None:
            selected = selected.where(condition)
    fields = {"record": builder.pk_name} if chart.raw else {"x": chart.dimension.field}
    if not chart.raw and chart.group:
        fields["group"] = chart.group.field
    for parameter, name in fields.items():
        try:
            value = TypeAdapter(builder.specs[name].annotation).validate_python(key[parameter])
        except (KeyError, ValidationError) as exc:
            raise BuilderValidationError({"selector": ["Invalid chart selection."]}) from exc
        selected = selected.where(
            getattr(builder.model, name) == builder.specs[name].normalize(value)
        )
    ids = (
        selected.order_by(None)
        .with_only_columns(builder.pk, maintain_column_froms=True)
        .distinct()
        .order_by(builder.pk)
        .offset(offset)
        .limit(limit)
    )
    return list(
        (
            await ctx.session.scalars(
                service._statement("list", ctx)
                .where(builder.pk.in_(ids))
                .order_by(None)
                .order_by(builder.pk),
            )
        ).unique()
    )
