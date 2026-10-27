"""Typed column summaries over the complete filtered, permission-scoped list."""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from obox_builder.contrib.admin.context import AdminContext
from obox_builder.contrib.admin.list_query import list_queryset, load_query
from obox_builder.contrib.admin.options import ModelAdmin
from obox_builder.fields import FieldSpec, QuerySpecError

COMMON = (
    ("empty", "Empty"),
    ("filled", "Filled"),
    ("unique", "Unique"),
    ("percent_empty", "Percent empty"),
    ("percent_filled", "Percent filled"),
    ("percent_unique", "Percent unique"),
)
NUMERIC = (
    ("sum", "Sum"),
    ("average", "Average"),
    ("median", "Median"),
    ("min", "Min"),
    ("max", "Max"),
    ("range", "Range"),
    ("stddev", "Standard deviation"),
)


def summary_choices(spec: FieldSpec) -> tuple[tuple[str, str], ...]:
    if (
        spec.secret
        or spec.column is None
        or spec.options.get("computed")
        or spec.options.get("relationship_path")
        or spec.type.name in {"json", "multi_link"}
    ):
        return ()
    extra: tuple[tuple[str, str], ...] = ()
    if spec.type.name in {"integer", "decimal"} and not spec.primary_key:
        extra = NUMERIC
    elif spec.type.name in {"date", "datetime", "time"}:
        extra = (("min", "Earliest"), ("max", "Latest"))
    elif spec.type.name == "checkbox":
        extra = (
            ("checked", "Checked"),
            ("unchecked", "Unchecked"),
            ("percent_checked", "Percent checked"),
        )
    return (("none", "None"), *extra, *COMMON)


async def summary_response(ctx: AdminContext) -> JSONResponse:
    assert ctx.admin is not None  # noqa: S101
    admin = ctx.admin
    admin.check_permission(ctx, "view")
    params = ctx.request.query_params
    spec = next((s for s in admin.get_list_specs(ctx) if s.name == params.get("field")), None)
    operation = params.get("operation", "none")
    if spec is None or operation not in dict(summary_choices(spec)):
        raise HTTPException(400, "Unsupported column summary.")
    try:
        query, _ = await load_query(ctx)
        stmt, _ = list_queryset(ctx, query, admin.get_query_specs(ctx))
    except QuerySpecError as exc:
        raise HTTPException(400, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    assert spec.column is not None  # noqa: S101
    value = None
    if operation != "none":
        if type(admin).has_view_permission is ModelAdmin.has_view_permission:
            value = await _sql_summary(ctx, stmt, spec, operation)
        else:
            # Custom object permissions must be evaluated before exposing any values.
            values = []
            seen = set()
            result = await ctx.db.stream_scalars(stmt.execution_options(yield_per=500))
            async for obj in result:
                pk = admin.get_pk(obj)
                if pk not in seen and admin.has_view_permission(replace(ctx, obj=obj), obj):
                    values.append(getattr(obj, spec.column.key or spec.name))
                seen.add(pk)
            value = _python_summary(values, operation)
    if value is None:
        text = "—"
    elif operation.startswith("percent_"):
        text = f"{value:.1f}%"
    elif isinstance(value, (int, float, Decimal)):
        text = f"{value:,}" if isinstance(value, int) else f"{value:,.2f}".rstrip("0").rstrip(".")
    else:
        text = spec.type.cell(value, spec).text
    return JSONResponse(
        jsonable_encoder({"value": value, "text": text}, custom_encoder={Decimal: str}),
        headers={"Cache-Control": "private, no-store"},
    )


async def _sql_summary(
    ctx: AdminContext, stmt: sa.Select[Any], spec: FieldSpec, operation: str
) -> Any:
    assert ctx.admin is not None  # noqa: S101
    assert spec.column is not None  # noqa: S101
    scope = (
        stmt.order_by(None)
        .with_only_columns(
            ctx.admin.pk_column().label("record_id"),
            spec.column.label("value"),
            maintain_column_froms=True,
        )
        .distinct()
        .subquery()
    )
    value = scope.c.value
    populated = (
        sa.func.nullif(sa.cast(value, sa.String), "")
        if isinstance(value.type, sa.String)
        else value
    )
    total = sa.func.count()
    filled = sa.func.count(populated)
    counts = {
        "empty": total - filled,
        "filled": filled,
        "unique": sa.func.count(sa.distinct(populated)),
        "checked": sa.func.count(sa.case((value.is_(True), 1))),
        "unchecked": sa.func.count(sa.case((value.is_(False), 1))),
    }
    expression: Any
    if operation.startswith("percent_"):
        expression = 100.0 * counts[operation.removeprefix("percent_")] / sa.func.nullif(total, 0)
        expression = sa.func.coalesce(expression, 0)
    elif operation in counts:
        expression = counts[operation]
    elif operation == "median":
        ranked = (
            sa.select(
                value,
                sa.func.row_number().over(order_by=value).label("position"),
                sa.func.count().over().label("size"),
            )
            .where(value.is_not(None))
            .subquery()
        )
        return await ctx.db.scalar(
            sa.select(sa.func.avg(ranked.c.value)).where(
                ranked.c.position.between(
                    (ranked.c.size + 1) // 2,
                    (ranked.c.size + 2) // 2,
                )
            )
        )
    elif operation == "stddev":
        mean = sa.select(sa.func.avg(value)).correlate(None).scalar_subquery()
        deviation = sa.cast(value, sa.Float) - mean
        variance = await ctx.db.scalar(sa.select(sa.func.avg(deviation * deviation)))
        return None if variance is None else math.sqrt(max(0, variance))
    else:
        expression = {
            "sum": sa.func.coalesce(sa.func.sum(value), 0),
            "average": sa.func.avg(value),
            "min": sa.func.min(value),
            "max": sa.func.max(value),
            "range": sa.func.max(value) - sa.func.min(value),
        }[operation]
    return await ctx.db.scalar(sa.select(expression).select_from(scope))


def _python_summary(values: list[Any], operation: str) -> Any:
    filled = [value for value in values if value is not None and value != ""]
    counts = {
        "empty": len(values) - len(filled),
        "filled": len(filled),
        "unique": len(set(filled)),
        "checked": sum(value is True for value in values),
        "unchecked": sum(value is False for value in values),
    }
    if operation.startswith("percent_"):
        return 100 * counts[operation.removeprefix("percent_")] / len(values) if values else 0
    if operation in counts:
        return counts[operation]
    if operation == "sum":
        return sum(filled)
    if not filled:
        return None
    functions: dict[str, Callable[[list[Any]], Any]] = {
        "average": statistics.mean,
        "median": statistics.median,
        "min": min,
        "max": max,
        "range": lambda items: max(items) - min(items),
        "stddev": statistics.pstdev,
    }
    return functions[operation](filled)
