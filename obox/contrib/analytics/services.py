"""Bounded analytical execution shared by JSON, HTML and Python consumers."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from time import monotonic
from typing import TYPE_CHECKING, Any, cast

import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError

from obox.components.declarations import (
    ComponentQuery,
    ComponentRecords,
    RecordResults,
)
from obox.contrib.analytics.aggregates import encode
from obox.contrib.analytics.pivot import PivotSpec
from obox.contrib.analytics.query import execute as chart_data
from obox.contrib.analytics.query import records as chart_records
from obox.contrib.analytics.query import selector
from obox.core.async_utils import invoke
from obox.core.exceptions import (
    BuilderValidationError,
    DomainError,
    NotFound,
)

if TYPE_CHECKING:
    from obox.components.service import ComponentService
    from obox.contrib.analytics.source import DataSource
    from obox.runtime.context import Context


class AggregateTimeout(DomainError):
    code = "aggregate_timeout"


logger = logging.getLogger(__name__)


def selection_binding(service: ComponentService, query: ComponentQuery, ctx: Context) -> str:
    return json.dumps(
        [service.identity, repr(service.spec), str(ctx.principal.id), repr(service.options(query))],
        sort_keys=True,
        default=str,
    )


async def timeout(service: ComponentService, ctx: Context) -> None:
    if ctx.session.get_bind().dialect.name == "postgresql":
        limits = service.app.feature("analytics")
        await ctx.session.execute(
            sa.select(sa.func.set_config("statement_timeout", str(limits.timeout_ms), True))
        )


def is_timeout(exc: DBAPIError) -> bool:
    return any(
        getattr(error, "sqlstate", getattr(error, "pgcode", None)) == "57014"
        for error in (exc.orig, getattr(exc.orig, "__cause__", None))
    )


async def execute(
    service: ComponentService, query: ComponentQuery, ctx: Context, component: Any
) -> dict[str, Any]:
    limits = service.app.feature("analytics")
    resource, spec = cast("DataSource", service.dataset), service.spec
    options = service.options(query)
    started, outcome = monotonic(), "error"
    try:
        await timeout(service, ctx)
        if component.kind != "pivot":
            spec = replace(spec, statement=component.get_statement)
            payload = await chart_data(
                resource,
                spec,
                ctx,
                max_rows=limits.max_pie_groups
                if spec.kind in {"pie", "donut"}
                else limits.max_chart_groups,
                max_groups=limits.max_columns,
                max_cells=limits.max_cells,
                max_points=limits.max_points,
                **options,
            )
            binding = selection_binding(service, query, ctx)
            for series in payload["series"]:
                for point in series["points"]:
                    key = point.pop("key")
                    if spec.drill:
                        point["selector"] = selector(service.app.csrf.secret, binding, key=key)
        else:
            bounds = {
                "max_rows": limits.max_rows,
                "max_columns": limits.max_columns,
                "max_cells": limits.max_cells,
            }
            payload = await resource.aggregate(spec, ctx=ctx, **options, **bounds)
        outcome = "ok"
        return payload
    except DBAPIError as exc:
        if not is_timeout(exc):
            raise
        outcome = "timeout"
        raise AggregateTimeout("Query timed out. Narrow the filters and try again.") from exc
    finally:
        logger.info(
            "component=%s duration_ms=%.1f outcome=%s",
            service.identity,
            (monotonic() - started) * 1000,
            outcome,
        )


async def records(
    service: ComponentService, query: ComponentRecords, ctx: Context, component: Any
) -> RecordResults:
    if not component.drill:
        raise NotFound("Record inspection is disabled.")
    resource, spec = cast("DataSource", service.dataset), service.spec
    options = service.options(query)
    try:
        await timeout(service, ctx)
        if component.kind != "pivot":
            if {"row", "column"}.intersection(query.model_fields_set):
                raise BuilderValidationError({"selector": ["Use a chart selection."]})
            key = selector(
                service.app.csrf.secret,
                selection_binding(service, query, ctx),
                token=query.selector,
            )
            objects = await chart_records(
                resource,
                replace(spec, statement=component.get_statement),
                ctx,
                key,
                offset=query.offset,
                limit=query.limit,
                **options,
            )
        else:
            if query.selector is not None:
                raise BuilderValidationError({"selector": ["Use pivot row and column keys."]})
            pivot_spec: PivotSpec = spec
            keys = {name: value for name, (_, value) in pivot_spec.selection(query).items()}
            objects = await resource.aggregate_records(
                pivot_spec, ctx=ctx, keys=keys, offset=query.offset, limit=query.limit, **options
            )
        dataset = resource.dataset
        readable = set(dataset.readable(ctx))
        from pydantic.alias_generators import to_camel

        permitted = [
            obj
            for obj in objects[: query.limit]
            if await invoke(component.has_object_permission, ctx, obj)
        ]
        values = [
            {
                to_camel(name): str(getattr(obj, name))
                if name == dataset.pk_name
                else encode(getattr(obj, name))
                for name in await invoke(component.get_record_fields, ctx, obj)
                if name in readable
            }
            for obj in permitted
        ]
        result = RecordResults(
            records=values,
            offset=query.offset,
            limit=query.limit,
            has_more=len(objects) == query.limit,
        )
        result._objects = permitted
        return result
    except DBAPIError as exc:
        if not is_timeout(exc):
            raise
        raise AggregateTimeout("Query timed out. Narrow the filters and try again.") from exc
