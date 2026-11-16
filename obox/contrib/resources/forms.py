"""Model form configuration, input conversion, validation and persistence."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Literal, cast

import sqlalchemy as sa

from obox._context import Context
from obox._exceptions import N_, BuilderValidationError
from obox.contrib.resources.service import invoke
from obox.contrib.resources.surface import _Presentation
from obox.db._fields import FieldRefs, check_fields, schema, validate


class FormOptions(_Presentation):
    enabled = True
    template = "obox/form.html"
    fields: FieldRefs | None = None
    fieldsets: Sequence[tuple[str | None, Mapping[str, Any]]] | None = None
    labels: Mapping[str, str] = {}
    help_texts: Mapping[str, str] = {}
    widgets: Mapping[str, Mapping[str, Any]] = {}

    def get_fields(
        self, ctx: Context, *, action: Literal["create", "update"], instance: Any = None
    ) -> Sequence[str]:
        return cast(Sequence[str], self.builder.writable(ctx, action, obj=instance, surface="form"))

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

    def get_initial_values(self, ctx: Context, names: Sequence[str]) -> dict[str, Any]:
        values = {
            key: self.builder.specs[key].column.default.arg
            for key in names
            if self.builder.specs[key].column.default is not None
            and self.builder.specs[key].column.default.is_scalar
        }
        values.update(self.get_initial(ctx))
        return values

    def convert_values(self, data: Mapping[str, Any], *, instance: Any = None) -> dict[str, Any]:
        converted = dict(data)
        for key, value in data.items():
            if key not in self.builder.specs:
                continue
            column = self.builder.specs[key].column
            if value == "" and column.nullable:
                converted[key] = None
            elif (
                value == ""
                and instance is None
                and column.type.python_type is not str
                and (column.default is not None or column.server_default is not None)
            ):
                converted.pop(key)
            elif isinstance(column.type, sa.JSON) and isinstance(value, str):
                try:
                    converted[key] = json.loads(value)
                except (TypeError, ValueError) as exc:
                    raise BuilderValidationError({key: [N_("Enter valid JSON.")]}) from exc
        return converted

    async def clean(
        self,
        data: Mapping[str, Any],
        ctx: Context,
        *,
        instance: Any = None,
        fields: Sequence[str] | None = None,
        partial: bool = False,
    ) -> dict[str, Any]:
        action: Literal["create", "update"] = "create" if instance is None else "update"
        permitted = self.builder.writable(ctx, action, obj=instance, surface="form")
        allowed = tuple(
            name
            for name in (
                self.get_fields(ctx, action=action, instance=instance) if fields is None else fields
            )
            if name in permitted
        )
        check_fields(data, allowed, self.builder.specs)
        cleaned = await invoke(
            self.validate, self.convert_values(data, instance=instance), ctx, instance=instance
        )
        check_fields(cleaned, allowed, self.builder.specs)
        parsed = validate(
            schema(
                "FormInput", self.builder.specs, allowed, partial=partial or instance is not None
            ),
            cleaned,
        )
        return {name: self.builder.specs[name].normalize(value) for name, value in parsed.items()}

    async def save(
        self,
        data: Mapping[str, Any],
        ctx: Context,
        *,
        instance: Any = None,
        precondition: Callable[[Any], Awaitable[None]] | None = None,
    ) -> Any:
        if instance is None:
            return await self.service.create(data, ctx=ctx, surface="form")
        return await self.service.update(
            getattr(instance, self.builder.pk_name),
            data,
            ctx=ctx,
            precondition=precondition,
            surface="form",
        )
