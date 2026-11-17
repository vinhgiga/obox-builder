"""Bounded analytical execution shared by JSON, HTML and Python consumers."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from time import monotonic
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError

from obox._async import invoke
from obox._components import ComponentQuery, ComponentRecords, RecordResults
from obox._exceptions import BuilderValidationError, NotFound
from obox.contrib.analytics import AggregateTimeout, Chart, Pivot
from obox.contrib.analytics.aggregates import encode
from obox.contrib.analytics.query import execute as chart_data
from obox.contrib.analytics.query import records as chart_records
from obox.contrib.analytics.query import selector
from obox.core.exceptions import ImproperlyConfigured

if TYPE_CHECKING:
    from obox._component_service import ComponentService
    from obox._context import Context

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


async def execute(
    service: ComponentService, query: ComponentQuery, ctx: Context, component: Chart | Pivot
) -> dict[str, Any]:
    limits = service.app._feature("analytics")
    resource, spec = service.dataset, service.spec
    if resource is None:
        raise ImproperlyConfigured("Analytics requires a source resource.")
    options = service.options(query)
    started, outcome = monotonic(), "error"
    try:
        await timeout(service, ctx)
        if isinstance(component, Chart):
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
            result = await resource.aggregate(
                spec.aggregation,
                ctx=ctx,
                **options,
                max_rows=limits.max_rows,
                max_columns=limits.max_columns,
                max_cells=limits.max_cells,
            )
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
    if resource is None:
        raise ImproperlyConfigured("Analytics requires a source resource.")
    options = service.options(query)
    try:
        await timeout(service, ctx)
        if isinstance(component, Chart):
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
            keys = {}
            for name, field in (("row", spec.rows), ("column", spec.columns)):
                if name in query.model_fields_set:
                    if field is None:
                        raise BuilderValidationError(
                            {name: ["This pivot has no column dimension."]}
                        )
                    keys[field] = getattr(query, name)
            objects = await resource.aggregate_records(
                spec.aggregation,
                ctx=ctx,
                keys=keys,
                offset=query.offset,
                limit=query.limit,
                **options,
            )
        dataset = resource.dataset
        readable = set(dataset.readable(ctx))
        from pydantic.alias_generators import to_camel

        permitted = [
            obj for obj in objects if await invoke(component.has_object_permission, ctx, obj)
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
