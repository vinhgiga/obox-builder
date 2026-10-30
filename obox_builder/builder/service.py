"""Authorized CRUD shared by HTTP, HTML and direct callers."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from types import MappingProxyType
from typing import Any, Generic, TypeVar

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import with_parent
from sqlalchemy.orm.exc import StaleDataError

from obox_builder.builder.fields import schema, validate
from obox_builder.context import Context, DomainEvent
from obox_builder.errors import (
    BuilderValidationError,
    ImproperlyConfigured,
    NotAuthenticated,
    NotFound,
    PermissionDenied,
    ResourceConflict,
)

T = TypeVar("T")


async def invoke(method: Any, *args: Any, **kwargs: Any) -> Any:
    result = method(*args, **kwargs)
    return await result if inspect.isawaitable(result) else result


class ResourceService(Generic[T]):
    def __init__(self, builder: Any, app: Any) -> None:
        self.builder, self.app = builder, app

    def _context(self, ctx: Context) -> None:
        if ctx._owner is not self.app or not ctx._active or ctx._failed:
            raise ImproperlyConfigured("Use an active, successful unit of work from this app.")
        if ctx.session.sync_session.get_transaction() is not ctx._transaction:
            ctx._failed = True
            raise ImproperlyConfigured("Hooks and services must not end the shared transaction.")

    async def authorize(self, action: str, ctx: Context, obj: Any = None) -> None:
        self._context(ctx)
        if not await invoke(self.builder.policy.allow, action, ctx, obj=obj):
            if not getattr(ctx.principal, "is_authenticated", False):
                raise NotAuthenticated("Authentication is required.")
            raise PermissionDenied("This operation is not permitted.")

    def _statement(self, action: str, ctx: Context) -> Any:
        self._context(ctx)
        return self.builder.get_statement(ctx).where(self.builder.policy.scope(action, ctx))

    async def list(
        self,
        *,
        ctx: Context,
        filters: Mapping[str, Any] | None = None,
        search: str | None = None,
        ordering: Sequence[str] | None = None,
        offset: int = 0,
        limit: int | None = None,
    ) -> Sequence[T]:
        await self.authorize("list", ctx)
        b = self.builder
        if (
            isinstance(offset, bool)
            or offset < 0
            or (limit is not None and (isinstance(limit, bool) or limit < 0))
        ):
            raise BuilderValidationError({"pagination": ["Use nonnegative offset and limit."]})
        readable = set(b.readable(ctx))
        if ctx.source == "api":
            readable.intersection_update(b.api_read)
        stmt = self._statement("list", ctx)
        for key, value in (filters or {}).items():
            if key not in b.filter_fields or key not in readable:
                raise BuilderValidationError({key: ["Filtering this field is not permitted."]})
            parsed = validate(schema("Filter", b.specs, (key,), partial=True), {key: value})
            stmt = stmt.where(getattr(b.model, key) == parsed[key])
        if search:
            names = tuple(key for key in b.search_fields if key in readable)
            if not names:
                raise BuilderValidationError({"search": ["Search is not permitted."]})
            stmt = stmt.where(
                sa.or_(*(getattr(b.model, key).icontains(search, autoescape=True) for key in names))
            )
        orders = (
            tuple(
                key
                for key in b.ordering
                if key.removeprefix("-") in readable or key.removeprefix("-") == b.pk_name
            )
            if ordering is None
            else tuple(ordering)
        )
        for order in orders:
            name = order.removeprefix("-")
            if ordering is not None and (name not in b.sort_fields or name not in readable):
                raise BuilderValidationError({"ordering": [f"Sorting {name!r} is not permitted."]})
        if b.pk_name not in {key.removeprefix("-") for key in orders}:
            orders = (*orders, b.pk_name)
        stmt = (
            stmt.order_by(None)
            .order_by(
                *(
                    getattr(b.model, key.removeprefix("-")).desc()
                    if key.startswith("-")
                    else getattr(b.model, key)
                    for key in orders
                )
            )
            .offset(offset)
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        return list((await ctx.session.scalars(stmt)).unique())

    async def _get(self, id: Any, ctx: Context, action: str) -> T:
        await self.authorize(action, ctx)
        b = self.builder
        try:
            key = validate(schema("Identity", b.specs, (b.pk_name,)), {b.pk_name: id})[b.pk_name]
        except BuilderValidationError as exc:
            raise NotFound("Record is unavailable.") from exc
        stmt = self._statement(action, ctx).where(getattr(b.model, b.pk_name) == key)
        if action in {"update", "delete"}:
            stmt = stmt.with_for_update(of=b.model).execution_options(populate_existing=True)
        obj = (await ctx.session.scalars(stmt)).unique().one_or_none()
        if obj is None:
            raise NotFound("Record is unavailable.")
        await self.authorize(action, ctx, obj)
        return obj

    async def get(self, id: Any, *, ctx: Context) -> T:
        return await self._get(id, ctx, "retrieve")

    async def related(self, id: Any, name: str, *, ctx: Context) -> Sequence[Any]:
        obj = await self.get(id, ctx=ctx)
        b = self.builder
        if name not in b.view_related:
            raise PermissionDenied("This collection has no declared read projection.")
        allowed = b.policy.read_fields(ctx, obj=obj)
        if allowed is not None and name not in allowed:
            raise PermissionDenied("This collection is not readable.")
        relation = sa.inspect(b.model).relationships[name]
        target = self.app.get_builder(relation.mapper.class_)
        target_service = self.app.service(target.model)
        await target_service.authorize("list", ctx)
        stmt = (
            target_service._statement("list", ctx)
            .where(with_parent(obj, getattr(b.model, name)))
            .order_by(target.pk)
        )
        return list((await ctx.session.scalars(stmt)).unique())

    def write_fields(self, action: str, ctx: Context, obj: Any = None) -> tuple[str, ...]:
        surface = "form" if ctx.source == "ui" else "api" if ctx.source == "api" else "service"
        allowed = self.builder.writable(ctx, action, obj=obj, surface=surface)
        if surface == "form":
            requested = self.builder.form.get_fields(ctx, action=action, instance=obj)
            allowed = tuple(key for key in allowed if key in requested)
        return allowed

    def _check_fields(self, data: Mapping[str, Any], allowed: Sequence[str]) -> None:
        unknown = set(data) - self.builder.specs.keys()
        if unknown:
            raise BuilderValidationError({key: ["Unknown field."] for key in sorted(unknown)})
        denied = set(data) - set(allowed)
        if denied:
            raise PermissionDenied(f"Fields cannot be written: {', '.join(sorted(denied))}.")

    async def _input(
        self, action: str, data: Mapping[str, Any], ctx: Context, obj: Any = None
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        b = self.builder
        allowed = self.write_fields(action, ctx, obj)
        self._check_fields(data, allowed)
        if ctx.source == "ui":
            data = await invoke(b.form.validate, dict(data), ctx, instance=obj)
            self._check_fields(data, allowed)
        parsed = validate(schema("Input", b.specs, allowed, partial=True), data)
        normalized = await (
            invoke(b.validate_create, parsed, ctx)
            if obj is None
            else invoke(b.validate_update, obj, parsed, ctx)
        )
        self._check_fields(normalized, allowed)
        parsed = validate(schema("Input", b.specs, allowed, partial=True), normalized)
        presets = dict(b.policy.presets(action, ctx))
        for key in presets:
            if key not in b.specs or b.specs[key].readonly:
                raise ImproperlyConfigured(f"Invalid server preset field: {key!r}.")
        presets = validate(schema("Presets", b.specs, presets, partial=True), presets)
        if action == "create":
            missing = {
                key: ["Field is required."]
                for key, spec in b.specs.items()
                if spec.required
                and not spec.readonly
                and key in allowed
                and key not in parsed
                and key not in presets
            }
            if missing:
                raise BuilderValidationError(missing)
        await self._relations(parsed, ctx)
        return {**parsed, **presets}, presets

    async def _relations(self, data: Mapping[str, Any], ctx: Context) -> None:
        for key, value in data.items():
            if value is None:
                continue
            for fk in self.builder.specs[key].column.foreign_keys:
                target = self.app._for_table(fk.column.table)
                if target is None:
                    raise PermissionDenied(f"Relationship {key!r} has no registered target policy.")
                service = self.app.service(target.model)
                await service.authorize("retrieve", ctx)
                remote = sa.inspect(target.model).get_property_by_column(fk.column).key
                stmt = service._statement("retrieve", ctx).where(
                    getattr(target.model, remote) == value
                )
                obj = (await ctx.session.scalars(stmt)).unique().one_or_none()
                if obj is None:
                    raise PermissionDenied("Related record is unavailable.")
                await service.authorize("retrieve", ctx, obj)
                if remote != target.pk_name and remote not in target.readable(ctx, obj=obj):
                    raise PermissionDenied("The relationship's reference field is not readable.")

    @asynccontextmanager
    async def _mutation(self, ctx: Context) -> AsyncIterator[None]:
        self._context(ctx)
        try:
            yield
            self._context(ctx)
        except (IntegrityError, StaleDataError) as exc:
            ctx._failed = True
            raise ResourceConflict("The write conflicts with current database state.") from exc
        except BaseException:
            ctx._failed = True
            raise

    async def _finish(self, action: str, obj: T, presets: Mapping[str, Any], ctx: Context) -> None:
        self._context(ctx)
        if action == "create":
            missing = {
                key: ["Field is required."]
                for key, spec in self.builder.specs.items()
                if spec.required and not spec.readonly and getattr(obj, key, None) is None
            }
            if missing:
                raise BuilderValidationError(missing)
        await ctx.session.flush()
        if not await invoke(self.builder.policy.check, action, obj, ctx):
            raise PermissionDenied("The resulting record is not permitted.")
        if any(getattr(obj, key) != value for key, value in presets.items()):
            raise PermissionDenied("A server-controlled field was changed.")
        self._context(ctx)
        ctx._events.append(
            DomainEvent(action, self.builder.model, getattr(obj, self.builder.pk_name))
        )

    async def create(self, data: Mapping[str, Any], *, ctx: Context) -> T:
        await self.authorize("create", ctx)
        values, presets = await self._input("create", data, ctx)
        async with self._mutation(ctx):
            obj = self.builder.model(**values)
            ctx.session.add(obj)
            await invoke(self.builder.before_create, obj, ctx)
            await self._finish("create", obj, presets, ctx)
            return obj

    async def update(
        self, id: Any, changes: Mapping[str, Any], *, ctx: Context, version: Any = None
    ) -> T:
        obj = await self._get(id, ctx, "update")
        self._version(obj, version)
        values, presets = await self._input("update", changes, ctx, obj)
        async with self._mutation(ctx):
            for key, value in values.items():
                setattr(obj, key, value)
            await invoke(self.builder.before_update, obj, MappingProxyType(values), ctx)
            await self._finish("update", obj, presets, ctx)
            return obj

    def _version(self, obj: T, version: Any) -> None:
        if version is None:
            return
        mapper = sa.inspect(self.builder.model)
        column = mapper.version_id_col
        if column is None:
            raise BuilderValidationError({"version": ["This model has no version counter."]})
        if getattr(obj, mapper.get_property_by_column(column).key) != version:
            raise ResourceConflict("The record has changed.")

    async def delete(self, id: Any, *, ctx: Context, version: Any = None) -> None:
        async with self._mutation(ctx):
            obj = await self._get(id, ctx, "delete")
            self._version(obj, version)
            await invoke(self.builder.before_delete, obj, ctx)
            self._context(ctx)
            await ctx.session.delete(obj)
            await ctx.session.flush()
            ctx._events.append(
                DomainEvent("delete", self.builder.model, getattr(obj, self.builder.pk_name))
            )

    async def serialize(
        self, obj: T, *, ctx: Context, fields: Sequence[str] | None = None
    ) -> dict[str, Any]:
        self._context(ctx)
        names = self.builder.readable(ctx, obj=obj, fields=fields)
        state = sa.inspect(obj)
        if state is None:
            raise ImproperlyConfigured("Serialization requires a mapped instance.")
        unloaded = state.unloaded.intersection(names)
        if unloaded:
            await ctx.session.refresh(obj, attribute_names=sorted(unloaded))
        return {key: getattr(obj, key) for key in names}
