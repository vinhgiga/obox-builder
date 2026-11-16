"""REST resource configuration and independently callable operations."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from typing import Any, cast

from fastapi import APIRouter
from pydantic import BaseModel

from obox._context import Context
from obox._exceptions import PermissionDenied
from obox.contrib.resources.api_schema import ApiRequest, ApiResponse
from obox.contrib.resources.surface import _Presentation
from obox.db._fields import FieldRefs, schema


class ApiOptions(_Presentation):
    enabled = True
    operations: tuple[str, ...] = ("list", "retrieve", "create", "update", "delete")
    read_fields: FieldRefs | None = None
    write_fields: FieldRefs | None = None
    prefix: str | None = None
    tags: tuple[str, ...] | None = None
    default_limit: int | None = None
    max_limit: int | None = None
    max_batch: int | None = None
    upsert_fields: tuple[tuple[str, ...], ...] = ()
    write_relationships: tuple[str, ...] = ()

    def get_read_schema(self, *, builder: Any) -> type[BaseModel]:
        return schema(
            builder.schema_name + "Read",
            builder.specs,
            builder.api_read,
            partial=True,
            base=ApiResponse,
            aliases=builder.api_names,
            string_fields=builder.api_identifiers,
        )

    def get_create_schema(self, *, builder: Any) -> type[BaseModel]:
        return schema(
            builder.schema_name + "Create",
            builder.specs,
            builder.api_write,
            conditional=builder.conditional_writes,
            base=ApiRequest,
            aliases=builder.api_names,
            string_fields=builder.api_identifiers,
        )

    def get_update_schema(self, *, builder: Any) -> type[BaseModel]:
        return schema(
            builder.schema_name + "Update",
            builder.specs,
            builder.api_write,
            partial=True,
            base=ApiRequest,
            aliases=builder.api_names,
            string_fields=builder.api_identifiers,
        )

    async def serialize(
        self, instance: Any, ctx: Context, *, fields: Sequence[str] | None = None
    ) -> dict[str, Any]:
        builder = self.builder
        selected = (
            builder.api_read
            if fields is None
            else tuple(name for name in fields if name in builder.api_read)
        )
        row = await self.service.serialize(instance, ctx=ctx, fields=selected)
        if builder.pk_name not in row:
            raise PermissionDenied("The primary key is not readable.")
        for name in builder.api_identifiers.intersection(row):
            if row[name] is not None:
                row[name] = str(row[name])
        return cast(dict[str, Any], row)

    async def list(self, pairs: Iterable[tuple[str, str]], ctx: Context) -> Any:
        from obox.contrib.resources.api import api_query
        from obox.contrib.resources.query import invalid

        builder = self.builder
        await self.service.authorize("list", ctx)
        query = api_query(builder, pairs)
        params = query.params
        selected = params.get("select", "*")
        fields = list(builder.api_read) if selected == "*" else selected.split(",")
        if any(name not in builder.api_read for name in fields):
            raise invalid("select", "Unknown ApiOptions field.", "unknown_field")
        if builder.pk_name not in fields:
            fields.append(builder.pk_name)
        objects, page = await self.service.page(query, ctx=ctx)
        rows = [await self.serialize(obj, ctx, fields=fields) for obj in objects]
        if "expand" in params:
            await self.service.expand(objects, rows, params["expand"].split(","), ctx=ctx)
        return {"items": rows, "page": page}

    async def retrieve(self, id: Any, ctx: Context) -> Any:
        return await self.service.get(id, ctx=ctx)

    async def create(self, data: Mapping[str, Any], ctx: Context) -> Any:
        return await self.service.create(data, ctx=ctx, surface="api")

    async def update(
        self,
        id: Any,
        data: Mapping[str, Any],
        ctx: Context,
        *,
        precondition: Callable[[Any], Awaitable[None]] | None = None,
    ) -> Any:
        return await self.service.update(
            id, data, ctx=ctx, precondition=precondition, surface="api"
        )

    async def delete(
        self,
        id: Any,
        ctx: Context,
        *,
        precondition: Callable[[Any], Awaitable[None]] | None = None,
    ) -> None:
        await self.service.delete(id, ctx=ctx, precondition=precondition)

    def build_router(self) -> APIRouter:
        from obox.contrib.resources.api import build_api

        return build_api(self.builder.app, self.builder, self.service)
