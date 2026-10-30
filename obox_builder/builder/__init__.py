"""One builder compiles the shared contract for Form, View and API."""

from __future__ import annotations

import inspect
import re
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any, Generic, TypeVar

import sqlalchemy as sa
from sqlalchemy.orm import raiseload, undefer

from obox_builder.auth import Policy, StaffOnly
from obox_builder.builder.fields import FieldRefs, inspect_fields, normalize
from obox_builder.builder.surfaces import API, Form, View
from obox_builder.context import Context, DomainEvent
from obox_builder.errors import ImproperlyConfigured

T = TypeVar("T")


class ModelBuilder(Generic[T]):
    model: type[T] | None = None
    slug: str | None = None
    fields: FieldRefs | None = None
    read_only_fields: FieldRefs = ()
    search_fields: FieldRefs = ()
    filter_fields: FieldRefs = ()
    sort_fields: FieldRefs | None = None
    ordering: tuple[str, ...] | None = None
    policy: Policy[T] | None = None
    Form = Form
    View = View
    API = API

    def _compile(self, model: type[T], default_policy: Policy[Any], app: Any) -> None:
        if self.model is not None and self.model is not model:
            raise ImproperlyConfigured("Builder.model differs from the registered model.")
        self.model, self.app = model, app
        self.policy = self.policy if self.policy is not None else default_policy
        if not isinstance(self.policy, Policy):
            raise ImproperlyConfigured("Builder.policy must be a Policy instance.")
        for name in ("scope", "read_fields", "write_fields", "presets"):
            if inspect.iscoroutinefunction(getattr(self.policy, name)):
                raise ImproperlyConfigured(f"Policy.{name} must be synchronous.")
        self.specs = MappingProxyType(inspect_fields(model))
        mapper: Any = sa.inspect(model)
        self.pk = mapper.primary_key[0]
        self.pk_name = mapper.get_property_by_column(self.pk).key
        if self.pk_name not in self.specs:
            raise ImproperlyConfigured("The primary key has an unsupported scalar type.")
        if self.fields is None and type(self.policy) is not StaffOnly:
            raise ImproperlyConfigured(
                "Resources using custom/public policies need explicit fields."
            )
        eligible = {key: spec for key, spec in self.specs.items() if spec.exposed}
        self.fields = normalize(
            self.fields if self.fields is not None else eligible, model, eligible
        )
        readonly = normalize(self.read_only_fields, model, self.specs)
        self.read_only_fields = tuple(
            key for key, spec in self.specs.items() if spec.readonly or key in readonly
        )
        for key in self.fields:
            if key not in self.read_only_fields and any(
                len(fk.constraint.elements) != 1 for fk in self.specs[key].column.foreign_keys
            ):
                raise ImproperlyConfigured("Composite foreign keys must be read-only.")
        name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", model.__name__).lower()
        self.slug = self.slug or (
            name[:-1] + "ies"
            if name.endswith("y") and name[-2:-1] not in "aeiou"
            else name + "es"
            if name.endswith(("s", "x", "z", "ch", "sh"))
            else name + "s"
        )
        self._validate_path(self.slug)
        for configured, base in ((self.Form, Form), (self.View, View), (self.API, API)):
            if not isinstance(configured, type) or not issubclass(configured, base):
                raise ImproperlyConfigured("Surface configuration must extend Form, View or API.")
        self.form, self.view, self.api = self.Form(), self.View(), self.API()
        self.form._builder = self.view._builder = self
        self.form_fields = self._projection(self.form.fields)
        self.view_columns = self._projection(self.view.columns)
        self.view_details = self._projection(self.view.detail_fields)
        self.view_related = normalize(
            self.view.related,
            model,
            {rel.key: rel for rel in mapper.relationships if rel.uselist},
        )
        self.api_read = self._projection(self.api.read_fields)
        self.api_write = tuple(
            key
            for key in self._projection(self.api.write_fields)
            if key not in self.read_only_fields
        )
        self.search_fields = self._projection(self.search_fields)
        self.filter_fields = self._projection(self.filter_fields)
        default_sort = tuple(
            key for key in self.fields if not isinstance(self.specs[key].column.type, sa.JSON)
        )
        self.sort_fields = self._projection(
            self.sort_fields if self.sort_fields is not None else default_sort
        )
        if any(
            not isinstance(self.specs[key].column.type, sa.String) for key in self.search_fields
        ):
            raise ImproperlyConfigured("Search fields must be string columns.")
        if any(isinstance(self.specs[key].column.type, sa.JSON) for key in self.sort_fields):
            raise ImproperlyConfigured("JSON fields cannot be sorted.")
        self.ordering = self.ordering if self.ordering is not None else (self.pk_name,)
        for order in self.ordering:
            if order.lstrip("-") not in (*self.sort_fields, self.pk_name):
                raise ImproperlyConfigured(f"Invalid ordering field: {order!r}.")
        if self.view.page_size is not None and self.view.page_size < 1:
            raise ImproperlyConfigured("View.page_size must be positive or None.")
        for actions, allowed in (
            (self.api.actions, {"list", "retrieve", "create", "update", "delete"}),
            (self.view.actions, {"create", "update", "delete"}),
        ):
            if len(set(actions)) != len(actions) or set(actions) - allowed:
                raise ImproperlyConfigured("Unknown or duplicate surface actions.")
        if self.api.prefix is not None:
            self._validate_path(self.api.prefix)
        for mapping in (self.form.labels, self.form.help_texts, self.form.widgets):
            if set(mapping) - set(self.form_fields):
                raise ImproperlyConfigured("Form configuration references unpublished fields.")
        if self.form.layout is not None:
            normalize(self.form.layout, model, {key: self.specs[key] for key in self.form_fields})
        self.conditional_writes = (
            type(self.policy).presets is not Policy.presets
            or type(self.policy).write_fields is not Policy.write_fields
        )
        self.read_schema = self.api.get_read_schema(builder=self)
        self.create_schema = self.api.get_create_schema(builder=self)
        self.update_schema = self.api.get_update_schema(builder=self)
        for compiled in (self.read_schema, self.create_schema, self.update_schema):
            compiled.model_json_schema()

    @staticmethod
    def _validate_path(value: str) -> None:
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]*", value):
            raise ImproperlyConfigured(f"Invalid resource path: {value!r}.")

    def _projection(self, refs: FieldRefs | None) -> tuple[str, ...]:
        if self.fields is None or self.model is None:
            raise ImproperlyConfigured("Register the builder before using field projections.")
        selected = self.fields if refs is None else refs
        names = normalize(selected, self.model, self.specs)
        return tuple(name for name in names if name in self.fields)

    def readable(
        self, ctx: Context, *, obj: T | None = None, fields: Sequence[str] | None = None
    ) -> tuple[str, ...]:
        if self.policy is None or self.fields is None:
            raise ImproperlyConfigured("Register the builder before using field projections.")
        allowed = self.policy.read_fields(ctx, obj=obj)
        return tuple(
            key
            for key in (self.fields if fields is None else fields)
            if key in self.fields and (allowed is None or key in allowed)
        )

    def writable(
        self, ctx: Context, action: str, *, obj: T | None = None, surface: str = "service"
    ) -> tuple[str, ...]:
        if self.policy is None or self.fields is None:
            raise ImproperlyConfigured("Register the builder before using field projections.")
        fields = (
            self.api_write
            if surface == "api"
            else (self.form_fields if surface == "form" else self.fields)
        )
        allowed = self.policy.write_fields(action, ctx, obj=obj)
        presets = self.policy.presets(action, ctx)
        readable = self.readable(ctx, obj=obj) if surface == "form" and action == "update" else None
        return tuple(
            key
            for key in fields
            if key not in self.read_only_fields
            and key not in presets
            and (allowed is None or key in allowed)
            and (readable is None or key in readable)
        )

    def get_statement(self, ctx: Context) -> sa.Select[tuple[T]]:
        if self.model is None:
            raise ImproperlyConfigured("Register the builder before querying its model.")
        return sa.select(self.model).options(undefer("*"), raiseload("*"))

    async def validate_create(self, data: dict[str, Any], ctx: Context) -> Mapping[str, Any]:
        return data

    async def validate_update(
        self, instance: T, changes: dict[str, Any], ctx: Context
    ) -> Mapping[str, Any]:
        return changes

    async def before_create(self, instance: T, ctx: Context) -> None:
        pass

    async def before_update(self, instance: T, changes: Mapping[str, Any], ctx: Context) -> None:
        pass

    async def before_delete(self, instance: T, ctx: Context) -> None:
        pass

    async def after_commit(self, event: DomainEvent, ctx: Context) -> None:
        pass


__all__ = ["API", "Form", "ModelBuilder", "View"]
