"""Presentation configuration and extension hooks; persistence stays in the service."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel

from obox_builder.builder.fields import FieldRefs, schema
from obox_builder.context import Context


class Form:
    enabled = True
    fields: FieldRefs | None = None
    fieldsets: Sequence[tuple[str | None, Mapping[str, Any]]] | None = None
    labels: Mapping[str, str] = {}
    help_texts: Mapping[str, str] = {}
    widgets: Mapping[str, Mapping[str, Any]] = {}
    _builder: Any

    def get_fields(
        self, ctx: Context, *, action: Literal["create", "update"], instance: Any = None
    ) -> Sequence[str]:
        return self._builder.writable(ctx, action, obj=instance, surface="form")

    def get_initial(self, ctx: Context) -> Mapping[str, Any]:
        return {}

    async def validate(
        self, data: Mapping[str, Any], ctx: Context, *, instance: Any = None
    ) -> Mapping[str, Any]:
        return data

    def get_context(
        self, ctx: Context, *, instance: Any = None, action: Literal["create", "update"]
    ) -> Mapping[str, Any]:
        return {}


class View:
    enabled = True
    columns: FieldRefs | None = None
    detail_fields: FieldRefs | None = None
    related: FieldRefs = ()
    operations: tuple[str, ...] = ("create", "update", "delete")
    page_size: int | None = None
    _builder: Any

    def get_columns(self, ctx: Context) -> Sequence[str]:
        return self._builder.readable(ctx, fields=self._builder.view_columns)

    def get_filters(self, ctx: Context) -> Sequence[str]:
        allowed = self._builder.readable(ctx)
        return tuple(name for name in self._builder.filter_fields if name in allowed)

    def get_operations(self, ctx: Context, *, instance: Any = None) -> Sequence[str]:
        return self.operations

    def get_context(self, ctx: Context, *, instance: Any = None) -> Mapping[str, Any]:
        return {}


class API:
    enabled = True
    operations: tuple[str, ...] = ("list", "retrieve", "create", "update", "delete")
    read_fields: FieldRefs | None = None
    write_fields: FieldRefs | None = None
    prefix: str | None = None
    tags: tuple[str, ...] | None = None
    pagination = False

    def get_read_schema(self, *, builder: Any) -> type[BaseModel]:
        return schema(
            builder.model.__name__ + "Read", builder.specs, builder.api_read, partial=True
        )

    def get_create_schema(self, *, builder: Any) -> type[BaseModel]:
        return schema(
            builder.model.__name__ + "Create",
            builder.specs,
            builder.api_write,
            conditional=builder.conditional_writes,
        )

    def get_update_schema(self, *, builder: Any) -> type[BaseModel]:
        return schema(
            builder.model.__name__ + "Update", builder.specs, builder.api_write, partial=True
        )
