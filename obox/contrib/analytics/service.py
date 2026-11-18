"""Bounded analytical execution shared by JSON, HTML and Python consumers."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from itertools import islice
from time import monotonic
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from starlette.concurrency import run_in_threadpool

from obox._async import invoke
from obox._components import ComponentQuery, ComponentRecords, RecordResults, fixed_filters
from obox._exceptions import BuilderValidationError, NotFound, PermissionDenied
from obox.contrib.analytics import AggregateTimeout, Chart, Pivot
from obox.contrib.analytics.aggregates import encode
from obox.contrib.analytics.pivot import Member, PivotSpec
from obox.contrib.analytics.pivot_memory import MemoryRows, execute_memory
from obox.contrib.analytics.query import execute as chart_data
from obox.contrib.analytics.query import records as chart_records
from obox.contrib.analytics.query import selector
from obox.core.exceptions import ImproperlyConfigured

if TYPE_CHECKING:
    from collections.abc import Iterator

    from obox._component_service import ComponentService
    from obox._context import Context
    from obox.contrib.analytics.pivot_memory import Row

logger = logging.getLogger(__name__)


def selection_binding(service: ComponentService, query: ComponentQuery, ctx: Context) -> str:
    return json.dumps(
        [service.identity, repr(service.spec), str(ctx.principal.id), repr(service.options(query))],
        sort_keys=True,
        default=str,
    )


async def timeout(service: ComponentService, ctx: Context) -> None:
    if ctx.session.get_bind().dialect.name == "postgresql":
        limits = service.app._feature("analytics")
        await ctx.session.execute(
            sa.select(sa.func.set_config("statement_timeout", str(limits.timeout_ms), True))
        )


def is_timeout(exc: DBAPIError) -> bool:
    return any(
        getattr(error, "sqlstate", getattr(error, "pgcode", None)) == "57014"
        for error in (exc.orig, getattr(exc.orig, "__cause__", None))
    )


def memory_rows(
    component: Pivot,
    spec: PivotSpec,
    ctx: Context,
    options: dict[str, Any],
    keys: dict[str, Member] | None = None,
) -> Iterator[Row]:
    source = component.data
    if not isinstance(source, MemoryRows):
        raise ImproperlyConfigured("Declare in-memory data for this pivot.")
    readable = set(component.get_fields(ctx))
    if not readable.issubset(component.fields):
        raise PermissionDenied("Dynamic component fields must narrow its declared fields.")
    if not set(spec.fields).issubset(readable):
        raise PermissionDenied("These fields are not available for this component.")
    return source.matching(
        readable=readable,
        filter_fields=component.fields
        if component.filter_fields is None
        else component.filter_fields,
        fixed=fixed_filters(component.filters),
        keys=keys,
        **options,
    )


async def execute(
    service: ComponentService, query: ComponentQuery, ctx: Context, component: Chart | Pivot
) -> dict[str, Any]:
    limits = service.app._feature("analytics")
    resource, spec = service.dataset, service.spec
    options = service.options(query)
    started, outcome = monotonic(), "error"
    try:
        if resource is not None:
            await timeout(service, ctx)
        if isinstance(component, Chart):
            if resource is None:
                raise ImproperlyConfigured("Charts require a source model.")
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
            if resource is None:
                payload = await run_in_threadpool(
                    execute_memory, memory_rows(component, spec, ctx, options), spec, **bounds
                )
            elif spec.legacy is None:
                payload = await resource.aggregate(spec, ctx=ctx, **options, **bounds)
            else:
                result = await resource.aggregate(spec.legacy, ctx=ctx, **options, **bounds)
                payload = result.to_dict()
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
    service: ComponentService, query: ComponentRecords, ctx: Context, component: Chart | Pivot
) -> RecordResults:
    if not component.drill:
        raise NotFound("Record inspection is disabled.")
    resource, spec = service.dataset, service.spec
    options = service.options(query)
    try:
        if resource is not None:
            await timeout(service, ctx)
        model_records = resource is not None
        if isinstance(component, Chart):
            if resource is None:
                raise ImproperlyConfigured("Charts require a source model.")
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
            aggregation = pivot_spec.legacy
            if resource is None:
                rows = memory_rows(component, pivot_spec, ctx, options, pivot_spec.selection(query))
                objects = await run_in_threadpool(
                    lambda: list(islice(rows, query.offset, query.offset + query.limit + 1))
                )
            else:
                keys: dict[str, Any] = {}
                if aggregation is None:
                    keys = {name: value for name, (_, value) in pivot_spec.selection(query).items()}
                else:
                    for name, field in (("row", aggregation.rows), ("column", aggregation.columns)):
                        if name in query.model_fields_set:
                            if field is None:
                                raise BuilderValidationError(
                                    {name: ["This pivot has no column dimension."]}
                                )
                            keys[field] = getattr(query, name)
                objects = await resource.aggregate_records(
                    aggregation or pivot_spec,
                    ctx=ctx,
                    keys=keys,
                    offset=query.offset,
                    limit=query.limit,
                    **options,
                )
        dataset = resource.dataset if resource else None
        readable = set(dataset.readable(ctx) if dataset else component.get_fields(ctx))
        from pydantic.alias_generators import to_camel

        permitted = [
            obj
            for obj in objects[: query.limit]
            if await invoke(component.has_object_permission, ctx, obj)
        ]
        values = [
            {
                to_camel(name): str(getattr(obj, name))
                if dataset is not None and name == dataset.pk_name
                else encode(getattr(obj, name) if model_records else obj.get(name))
                for name in await invoke(component.get_record_fields, ctx, obj)
                if name in readable
            }
            for obj in permitted
        ]
        result = RecordResults(
            records=values,
            offset=query.offset,
            limit=query.limit,
            has_more=len(objects) == query.limit if model_records else len(objects) > query.limit,
        )
        result._objects = permitted
        return result
    except DBAPIError as exc:
        if not is_timeout(exc):
            raise
        raise AggregateTimeout("Query timed out. Narrow the filters and try again.") from exc
