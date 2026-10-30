"""Import policy, shared by Python callers and the admin adapter."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import replace
from functools import cached_property
from typing import Any

import sqlalchemy as sa

from obox_builder.exceptions import ImproperlyConfigured, PermissionDenied
from obox_builder.fields import FieldSpec, RecordContext
from obox_builder.forms import BoundForm, build_schema
from obox_builder.resources import ModelResource, OperationContext
from obox_builder.utils.strings import camel_to_snake


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


class ImportResource:
    def __init__(
        self,
        resource: ModelResource,
        ctx: OperationContext,
        *,
        keys: Sequence[str] | None = None,
        specs: Sequence[FieldSpec] | None = None,
    ) -> None:
        self.resource = resource
        self.ctx = ctx.for_resource(resource)
        self.model = model = resource.model
        self.keys = tuple(resource.import_keys if keys is None else keys)
        source = specs if specs is not None else resource.specs.values()
        self.specs = [
            replace(spec, readonly=False, editable=True) if spec.primary_key else spec
            for spec in source
            if spec.type.name != "multi_link"
            and (
                (spec.is_editable() and not spec.primary_key)
                or (spec.primary_key and spec.name in self.keys and not spec.secret)
            )
        ]
        self.label = f"{model._model_label}.{camel_to_snake(model.__name__)}"
        self.columns = {spec.name: spec for spec in self.specs}
        self.schema = build_schema(self.specs)
        self.key_specs = [
            replace(self.columns[key], required=True) for key in self.keys if key in self.columns
        ]
        self.key_schema = build_schema(self.key_specs)
        generated = sa.inspect(model).local_table.autoincrement_column
        self.generated_key = (
            self.keys[0]
            if len(self.keys) == 1 and generated is not None and self.keys[0] == generated.key
            else None
        )

    @property
    def user(self):
        return self.ctx.actor

    def check_configuration(self, mode: str) -> None:
        if mode not in {"insert", "upsert", "update-only", "skip-existing"}:
            raise ValueError("Unknown import mode.")
        if mode != "insert" and not self.keys:
            raise ImproperlyConfigured("Configure import keys for this mode.")
        if not self.keys:
            return
        if len(set(self.keys)) != len(self.keys) or any(
            key not in self.columns for key in self.keys
        ):
            raise ImproperlyConfigured("Import keys must be distinct editable column fields.")
        table = sa.inspect(self.model).local_table
        unique = [set(table.primary_key.columns.keys())]
        unique += [
            set(c.columns.keys()) for c in table.constraints if isinstance(c, sa.UniqueConstraint)
        ]
        unique += [
            set(i.columns.keys())
            for i in table.indexes
            if i.unique
            and all(isinstance(expression, sa.Column) for expression in i.expressions)
            and not any(options.get("where") is not None for options in i.dialect_options.values())
        ]
        if set(self.keys) not in unique:
            raise ImproperlyConfigured(
                "Import keys must have an unconditional database unique constraint."
            )
        if any(self.columns[key].column is None for key in self.keys):
            raise ImproperlyConfigured("Import keys must refer to database columns.")

    def queryset(self) -> sa.Select[Any]:
        return self.resource.scoped_query(self.ctx, self.resource.operations["list"])

    def can_add(self) -> bool:
        return self.resource.can_execute(self.ctx, "create")

    def can_change(self, obj: Any = None) -> bool:
        return self.resource.can_execute(self.ctx, "update", obj)

    def check_permissions(self, mode: str) -> None:
        if not getattr(self.user, "is_active", False):
            raise PermissionDenied("The importing user is inactive.")
        if mode != "update-only" and not self.can_add():
            raise PermissionDenied("Adding records is not permitted.")
        if mode in {"upsert", "update-only"} and not self.can_change():
            raise PermissionDenied("Updating records is not permitted.")

    def context(self, obj: Any = None) -> RecordContext:
        return self.ctx.record_context(obj)

    def value_errors(self, values: dict[str, Any]) -> dict[str, list[str]]:
        errors: dict[str, list[str]] = {}
        for name, value in values.items():
            column_type = getattr(self.columns[name].column, "type", None)
            if isinstance(column_type, sa.Integer) and isinstance(value, int):
                bits = (
                    64
                    if isinstance(column_type, sa.BigInteger)
                    else 16
                    if isinstance(column_type, sa.SmallInteger)
                    else 32
                )
                if not -(2 ** (bits - 1)) <= value < 2 ** (bits - 1):
                    errors[name] = ["The integer is outside the database column's range."]
        return errors

    def bind(self, data: dict[str, Any], obj: Any = None) -> BoundForm:
        # Missing columns retain existing values. Blank mapped cells remain explicit.
        payload: dict[str, Any] = {}
        context = self.context(obj)
        for spec in self.specs:
            if obj is not None:
                attr = (
                    str(spec.column.key)
                    if spec.type.name == "link" and spec.column is not None
                    else spec.name
                )
                payload[spec.name] = getattr(obj, attr, None)
            elif spec.initial is not None:
                payload[spec.name] = (
                    spec.initial(context) if callable(spec.initial) else spec.initial
                )
        payload.update(data)
        for spec in self.specs:
            value = payload.get(spec.name)
            if spec.type.name in {"text", "long_text", "email", "url", "phone"} and isinstance(
                value, (int, float)
            ):
                payload[spec.name] = str(value)
        bound = BoundForm.bind(self.specs, self.schema, payload, base=context)
        bound.errors.update(self.value_errors(bound.cleaned))
        condition = replace(context, values=payload)
        for spec in self.specs:
            if spec.name not in data or spec.name in bound.errors:
                continue
            value = bound.cleaned.get(spec.name)
            if not spec.nullable and value is None:
                bound.errors.setdefault(spec.name, []).append("This field cannot be cleared.")
            limit = getattr(getattr(spec.column, "type", None), "length", None)
            if isinstance(value, str) and limit and len(value) > limit:
                bound.errors[spec.name] = [f"Use at most {limit} characters."]
            if (
                spec.name in data
                and spec.editable_if is not None
                and not spec.editable_if(condition)
            ):
                bound.errors[spec.name] = ["This field is not editable for this record."]
        return bound

    @cached_property
    def signature(self) -> str:
        return digest(
            [
                self.label,
                self.keys,
                [
                    (
                        s.name,
                        s.type.name,
                        s.required,
                        s.nullable,
                        dict(s.options),
                        str(s.column.type) if s.column is not None else None,
                    )
                    for s in self.specs
                ],
            ]
        )

    def snapshot(self, obj: Any) -> str:
        return digest(
            {column.key: getattr(obj, column.key) for column in sa.inspect(self.model).columns}
        )

    async def save(self, db, bound, target=None):
        context = replace(self.ctx, db=db)
        payload = {
            name: self.resource.specs[name].type.to_api(value, self.resource.specs[name])
            for name, value in bound.cleaned.items()
            if name in self.resource.specs and self.resource.specs[name].is_editable()
        }
        return await self.resource.execute(
            "create" if target is None else "update", context, payload, target=target
        )
