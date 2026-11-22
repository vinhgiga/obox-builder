"""Read-only analytical selections; no CRUD registration is required."""

from typing import Any

import sqlalchemy as sa

from obox._async import invoke
from obox._exceptions import BuilderValidationError, PermissionDenied
from obox.contrib._models import authorize
from obox.contrib.analytics.aggregates import execute
from obox.contrib.analytics.filters import _filter_condition, _filter_tree
from obox.contrib.analytics.pivot import PivotSpec, execute_sql
from obox.core.exceptions import ImproperlyConfigured
from obox.db import _scope
from obox.db._fields import inspect_fields


class Dataset:
    def __init__(self, component: Any) -> None:
        self.component, self.model = component, component.source
        mapper = sa.inspect(self.model, raiseerr=False)
        if mapper is None or len(mapper.primary_key) != 1:
            raise ImproperlyConfigured("Charts require a mapped model with one primary key.")
        self.pk = mapper.primary_key[0]
        self.pk_name = mapper.get_property_by_column(self.pk).key
        self.specs = inspect_fields(self.model)
        self.fields = tuple(component.fields)
        if not self.fields or set(self.fields) - self.specs.keys():
            raise ImproperlyConfigured("Declare the model fields available to this component.")
        self.filter_fields = (
            component.filter_fields if component.filter_fields is not None else self.fields
        )
        self.search_fields = tuple(
            name for name in self.fields if isinstance(self.specs[name].column.type, sa.String)
        )
        self.ordering = component.ordering

    def readable(self, ctx: Any) -> tuple[str, ...]:
        names = tuple(self.component.get_fields(ctx))
        if set(names) - set(self.fields):
            raise ImproperlyConfigured("Dynamic component fields must narrow its declared fields.")
        return names


class DataSource:
    def __init__(self, component: Any, app: Any, filters: Any) -> None:
        self.dataset, self.app, self.filters = Dataset(component), app, filters

    def _context(self, ctx: Any) -> None:
        if ctx._owner is not self.app or not ctx._active or ctx._failed:
            raise ImproperlyConfigured("Use an active unit of work from this application.")

    async def authorize(self, action: str, ctx: Any, obj: Any = None) -> None:
        self._context(ctx)
        authorize(self.dataset.model, ctx.principal, "view")
        if not await invoke(self.dataset.component.has_permission, ctx):
            raise PermissionDenied("This component is not permitted.")

    def _statement(self, action: str, ctx: Any) -> Any:
        self._context(ctx)
        stmt = self.dataset.component.get_queryset(ctx)
        if (
            not isinstance(stmt, sa.Select)
            or stmt.column_descriptions[0].get("entity") is not self.dataset.model
        ):
            raise ImproperlyConfigured("Component.get_queryset must return select(source_model).")
        stmt = _scope(stmt, self.dataset.model)
        condition = _filter_tree(
            self.dataset,
            set(self.dataset.readable(ctx)),
            self.filters,
            ctx.session.get_bind().dialect.name,
        )
        return stmt.where(condition) if condition is not None else stmt

    async def _list_statement(
        self,
        *,
        ctx: Any,
        filters: Any = None,
        filter_ops: Any = None,
        filter_group: Any = None,
        search: Any = None,
    ) -> Any:
        readable = set(self.dataset.readable(ctx))
        stmt = self._statement("list", ctx)
        for name, value in (filters or {}).items():
            stmt = stmt.where(
                _filter_condition(
                    self.dataset, readable, name, (filter_ops or {}).get(name, "is"), value
                )
            )
        if filter_group is not None:
            condition = _filter_tree(
                self.dataset, readable, filter_group, ctx.session.get_bind().dialect.name
            )
            if condition is not None:
                stmt = stmt.where(condition)
        if search:
            columns = [
                getattr(self.dataset.model, name)
                for name in self.dataset.search_fields
                if name in readable
            ]
            if not columns:
                raise BuilderValidationError({"search": ["Search is not available."]})
            stmt = stmt.where(
                sa.or_(*(column.icontains(search, autoescape=True) for column in columns))
            )
        return stmt, readable

    async def _aggregate_statement(
        self, query: Any, ctx: Any, filter_group: Any, search: Any
    ) -> Any:
        await self.authorize("list", ctx)
        stmt, readable = await self._list_statement(
            ctx=ctx, filter_group=filter_group, search=search
        )
        if not set(query.fields) <= readable:
            raise PermissionDenied("These fields are not available for aggregation.")
        return stmt

    async def aggregate(
        self, query: Any, *, ctx: Any, filter_group: Any = None, search: Any = None, **limits: Any
    ) -> Any:
        stmt = await self._aggregate_statement(query, ctx, filter_group, search)
        bounds = {"max_rows": 200, "max_columns": 50, "max_cells": 10000, **limits}
        if isinstance(query, PivotSpec):
            return await execute_sql(stmt, self.dataset, ctx.session, query, **bounds)
        return await execute(stmt, self.dataset, ctx.session, query, **bounds)

    async def aggregate_records(
        self,
        query: Any,
        *,
        ctx: Any,
        keys: Any,
        filter_group: Any = None,
        search: Any = None,
        offset: int = 0,
        limit: int = 50,
    ) -> Any:
        from pydantic import TypeAdapter, ValidationError

        stmt = await self._aggregate_statement(query, ctx, filter_group, search)
        dimensions = (
            set(query.rows + query.columns)
            if isinstance(query, PivotSpec)
            else {query.rows, query.columns}
        )
        if not set(keys) <= dimensions:
            raise BuilderValidationError({"keys": ["Unknown grouping field."]})
        for name, value in keys.items():
            spec = self.dataset.specs[name]
            try:
                parsed = (
                    TypeAdapter(spec.annotation).validate_python(value)
                    if value is not None
                    else None
                )
            except ValidationError as exc:
                raise BuilderValidationError({name: ["Invalid group key."]}) from exc
            stmt = stmt.where(getattr(self.dataset.model, name) == spec.normalize(parsed))
        pk = self.dataset.pk
        ids = (
            stmt.order_by(None)
            .with_only_columns(pk, maintain_column_froms=True)
            .distinct()
            .order_by(pk)
            .offset(offset)
            .limit(limit)
        )
        return list(
            (
                await ctx.session.scalars(
                    self._statement("list", ctx).where(pk.in_(ids)).order_by(None).order_by(pk)
                )
            ).unique()
        )
