"""One builder compiles the shared contract for Form, View and API."""

from __future__ import annotations

import inspect
import operator
import re
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any, Generic, TypeVar, get_args, get_origin

import sqlalchemy as sa
from pydantic.alias_generators import to_camel, to_snake
from sqlalchemy.orm import raiseload, selectinload, undefer
from sqlalchemy.orm.interfaces import MANYTOONE
from sqlalchemy.sql.elements import BinaryExpression

from obox_builder.auth import RBAC, Policy, StaffOnly
from obox_builder.builder.actions import action
from obox_builder.builder.api_schema import ApiRequest, ApiResponse
from obox_builder.builder.fields import FieldRefs, inspect_fields, normalize
from obox_builder.builder.inlines import Inline
from obox_builder.builder.surfaces import API, Form, View
from obox_builder.context import Context, DomainEvent
from obox_builder.errors import ImproperlyConfigured

T = TypeVar("T")


def _substitute_typevar(value: Any, substitutions: Mapping[TypeVar, Any]) -> Any:
    seen: set[TypeVar] = set()
    while isinstance(value, TypeVar) and value in substitutions and value not in seen:
        seen.add(value)
        value = substitutions[value]
    return value


def _infer_model(builder: type[Any]) -> type[Any] | None:
    models: set[type[Any]] = set()

    def visit(
        current: type[Any], substitutions: Mapping[TypeVar, Any], ancestors: frozenset[type[Any]]
    ) -> None:
        if current in ancestors:
            return
        ancestors = ancestors | {current}
        bases = current.__dict__.get("__orig_bases__", current.__bases__)
        for base in bases:
            origin = get_origin(base)
            parent = origin if isinstance(origin, type) else base
            if not isinstance(parent, type) or not issubclass(parent, ModelBuilder):
                continue
            arguments = get_args(base)
            if parent is ModelBuilder:
                if len(arguments) == 1:
                    model = _substitute_typevar(arguments[0], substitutions)
                    if isinstance(model, type) and model is not Any:
                        models.add(model)
                continue
            parent_substitutions = dict(substitutions)
            parent_substitutions.update(
                {
                    parameter: _substitute_typevar(argument, substitutions)
                    for parameter, argument in zip(
                        getattr(parent, "__parameters__", ()), arguments, strict=False
                    )
                }
            )
            visit(parent, parent_substitutions, ancestors)

    visit(builder, {}, frozenset())
    return next(iter(models)) if len(models) == 1 else None


class ModelBuilder(Generic[T]):
    model: type[T] | None
    slug: str | None = None
    app_label: str | None = None
    label_field: str | None = None
    fields: FieldRefs | None = None
    read_only_fields: FieldRefs = ()
    search_fields: FieldRefs = ()
    filter_fields: FieldRefs = ()
    sort_fields: FieldRefs | None = None
    ordering: tuple[str, ...] | None = None
    policy: Policy[T] | None = None
    permissions: Sequence[str] = ()
    actions: Sequence[str] = ()
    inlines: Sequence[type[Inline[Any]]] = ()
    inline_instances: Sequence[Inline[Any]] = ()
    Form = Form
    View = View
    API = API

    @property
    def route_key(self) -> str:
        return f"{self.app_label}.{self.slug}" if self.app_label else str(self.slug)

    @property
    def schema_name(self) -> str:
        name = self.model.__name__ if self.model is not None else str(self.slug)
        return f"{self.app_label}.{name}" if self.app_label else name

    @property
    def api_key(self) -> str:
        slug = self.api.prefix or self.slug
        return f"{self.app_label}/{slug}" if self.app_label else str(slug)

    @property
    def ui_path(self) -> str:
        prefix = (
            self.app.apps[self.app_label].ui_path(self.app.settings)
            if self.app_label
            else self.app.settings.ui_prefix
        )
        return f"{prefix}/{self.slug}"

    @property
    def api_path(self) -> str:
        return f"{self.app.settings.api_prefix}/{self.api_key}"

    @classmethod
    def _model_type(cls) -> type[T] | None:
        return _infer_model(cls)

    def _compile(self, model: type[T], default_policy: Policy[Any], app: Any) -> None:
        self.model, self.app = model, app
        self.policy = self.policy if self.policy is not None else default_policy
        if not isinstance(self.policy, Policy):
            raise ImproperlyConfigured("Builder.policy must be a Policy instance.")
        if isinstance(self.policy, StaffOnly):
            raise ImproperlyConfigured("StaffOnly is no longer supported; use RBAC instead.")
        standard = {"list", "retrieve", "create", "update", "delete"}
        for names in (self.permissions, self.actions):
            if (
                isinstance(names, str)
                or any(
                    not isinstance(name, str) or not name.isidentifier() or name.startswith("_")
                    for name in names
                )
                or len(set(names)) != len(names)
                or standard.intersection(names)
            ):
                raise ImproperlyConfigured("Use unique, nonreserved permission/action names.")
        methods = {}
        for name in self.actions:
            method = getattr(self, name, None)
            metadata = getattr(method, "obox_action", None)
            if (
                not callable(method)
                or not isinstance(metadata, tuple)
                or len(metadata) != 2
                or not isinstance(metadata[0], str)
                or not metadata[0].strip()
                or metadata[1] not in (*standard, *self.permissions)
            ):
                raise ImproperlyConfigured("Actions need @action and a declared permission.")
            methods[name] = method
        self.action_methods = MappingProxyType(methods)
        aliases = [to_camel(name) for name in self.actions]
        for alias in aliases:
            self._validate_path(alias)
        if len(set(aliases)) != len(aliases) or set(aliases).intersection(
            {"query", "queryOptions", "queryMetadata", "describe", "batchWrite"}
        ):
            raise ImproperlyConfigured("Action aliases must be unique and nonreserved.")
        for name in ("scope", "read_fields", "write_fields", "presets"):
            if inspect.iscoroutinefunction(getattr(self.policy, name)):
                raise ImproperlyConfigured(f"Policy.{name} must be synchronous.")
        self.specs = MappingProxyType(inspect_fields(model))
        if (
            any("obox_files" in spec.column.info for spec in self.specs.values())
            and app.get_plugin("files") is None
        ):
            raise ImproperlyConfigured("Attachment collections require the Files plugin.")
        mapper: Any = sa.inspect(model)
        self.pk = mapper.primary_key[0]
        self.pk_name = mapper.get_property_by_column(self.pk).key
        if self.pk_name not in self.specs:
            raise ImproperlyConfigured("The primary key has an unsupported scalar type.")
        if self.fields is None:
            raise ImproperlyConfigured(
                "Resources need explicit fields, including those using RBAC or public policies."
            )
        eligible = {key: spec for key, spec in self.specs.items() if spec.exposed}
        self.fields = normalize(
            self.fields if self.fields is not None else eligible, model, eligible
        )
        if self.label_field is not None and (
            not isinstance(self.label_field, str) or self.label_field not in self.fields
        ):
            raise ImproperlyConfigured("label_field must be a published scalar field.")
        self.list_relations = MappingProxyType(
            {
                relation.key: relation
                for relation in mapper.relationships
                if relation.direction is MANYTOONE
            }
        )
        relation_fields = {}
        for relation_name, relation in self.list_relations.items():
            if len(relation.local_columns) == 1 and len(relation.remote_side) == 1:
                column = next(iter(relation.local_columns))
                relation_fields[relation_name] = mapper.get_property_by_column(column).key
        self.list_relation_fields = MappingProxyType(relation_fields)
        readonly = normalize(self.read_only_fields, model, self.specs)
        self.read_only_fields = tuple(
            key for key, spec in self.specs.items() if spec.readonly or key in readonly
        )
        for key in self.fields:
            if key not in self.read_only_fields and any(
                len(fk.constraint.elements) != 1 for fk in self.specs[key].column.foreign_keys
            ):
                raise ImproperlyConfigured("Composite foreign keys must be read-only.")
        name = to_snake(model.__name__)
        self.slug = self.slug or to_camel(
            name[:-1] + "ies"
            if name.endswith("y") and name[-2:-1] not in "aeiou"
            else name + "es"
            if name.endswith(("s", "x", "z", "ch", "sh"))
            else name + "s"
        )
        self._validate_path(self.slug)
        if isinstance(self.policy, RBAC):
            self.policy = self.policy.bind(f"{self.app_label or 'admin'}.{self.slug}")
        for configured, base in ((self.Form, Form), (self.View, View), (self.API, API)):
            if not isinstance(configured, type) or not issubclass(configured, base):
                raise ImproperlyConfigured("Surface configuration must extend Form, View or API.")
        self.form, self.view, self.api = self.Form(), self.View(), self.API()
        table = mapper.local_table
        unique = {
            frozenset(mapper.get_property_by_column(column).key for column in constraint.columns)
            for constraint in table.constraints
            if isinstance(constraint, sa.UniqueConstraint) and not constraint.deferrable
        }
        unique.update(
            frozenset(mapper.get_property_by_column(column).key for column in index.columns)
            for index in table.indexes
            if index.unique
            and all(isinstance(expression, sa.Column) for expression in index.expressions)
            and all(
                index.dialect_options[dialect].get("where") is None
                for dialect in ("postgresql", "sqlite")
            )
        )
        for names in self.api.upsert_fields:
            if (
                not names
                or len(set(names)) != len(names)
                or frozenset(names) not in unique
                or any(
                    name not in self.specs
                    or self.specs[name].readonly
                    or self.specs[name].column.nullable
                    or isinstance(self.specs[name].column.type, sa.JSON | sa.ARRAY)
                    for name in names
                )
            ):
                raise ImproperlyConfigured(
                    "API.upsert_fields must name non-null writable columns of a full UNIQUE key."
                )
        for name in self.api.write_relationships:
            relation = mapper.relationships.get(name)
            if (
                relation is None
                or not relation.uselist
                or relation.viewonly
                or relation.collection_class not in (None, list)
                or len(relation.synchronize_pairs) != 1
                or (relation.secondary is None and len(relation.local_remote_pairs) != 1)
                or not isinstance(relation.primaryjoin, BinaryExpression)
                or relation.primaryjoin.operator is not operator.eq
                or (
                    relation.secondary is not None
                    and (
                        not isinstance(relation.secondary, sa.Table)
                        or len(relation.secondary.c) != 2
                        or len(relation.secondary.primary_key.columns) != 2
                        or len(relation.secondary_synchronize_pairs) != 1
                        or not isinstance(relation.secondaryjoin, BinaryExpression)
                        or relation.secondaryjoin.operator is not operator.eq
                    )
                )
                or "delete-orphan" in relation.cascade
            ):
                raise ImproperlyConfigured(
                    "API.write_relationships requires simple writable collections without "
                    "delete-orphan; many-to-many tables need a two-column primary key."
                )
            if mapper.version_id_col is not None and mapper.version_id_generator is False:
                raise ImproperlyConfigured("Relationship writes require generated versions.")
        for name in ("default_limit", "max_limit", "max_batch"):
            if not any(name in cls.__dict__ for cls in self.API.__mro__ if cls is not API):
                setattr(self.api, name, getattr(app.settings, "api_" + name))
        for value in (self.api.default_limit, self.api.max_limit, self.api.max_batch):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ImproperlyConfigured("API limits must be nonnegative integers or None.")
        if (
            self.api.max_limit is not None
            and self.api.default_limit is not None
            and self.api.default_limit > self.api.max_limit
        ):
            raise ImproperlyConfigured("API.default_limit must not exceed API.max_limit.")
        self.form._builder = self.view._builder = self
        instances: list[Inline[Any]] = []
        for inline_class in self.inlines:
            if not isinstance(inline_class, type) or not issubclass(inline_class, Inline):
                raise ImproperlyConfigured("inlines must extend Inline.")
            instance = inline_class()
            instance._compile(self)
            if any(item.model is instance.model for item in instances):
                raise ImproperlyConfigured("Duplicate inline model.")
            instances.append(instance)
        self.inline_instances = tuple(instances)
        self.form_fields = self._form_fields()
        self.view_list_display = self._list_projection(self.view.list_display)
        self.view_list_relations = tuple(
            name for name in self.view_list_display if name in self.list_relation_fields
        )
        self.view_details = self._projection(self.view.detail_fields)
        self.view_related = normalize(
            self.view.related,
            model,
            {rel.key: rel for rel in mapper.relationships if rel.uselist},
        )
        self.api_read = self._projection(self.api.read_fields)
        if self.api.enabled:
            if not self.specs[self.pk_name].exposed:
                raise ImproperlyConfigured("API resources require a published primary key.")
            self.api_read = tuple(dict.fromkeys((self.pk_name, *self.api_read)))
        self.api_names = MappingProxyType(
            {
                name: "id" if name == self.pk_name else to_camel(name)
                for name in (self.pk_name, *self.fields)
            }
        )
        if self.api.enabled and len(set(self.api_names.values())) != len(self.api_names):
            raise ImproperlyConfigured("API field aliases must be unique camelCase names.")
        self.api_identifiers = frozenset(
            name
            for name, spec in self.specs.items()
            if name == self.pk_name or spec.column.foreign_keys
        )
        self.version_column = mapper.version_id_col
        if self.view.inline_edit and self.version_column is None:
            raise ImproperlyConfigured("Inline editing requires version_id_col.")
        if (
            self.api.enabled
            and self.version_column is not None
            and (
                self.version_column.nullable
                or mapper.get_property_by_column(self.version_column).key not in self.specs
            )
        ):
            raise ImproperlyConfigured(
                "API version counters must be supported, non-nullable fields."
            )
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
        for operations, allowed in (
            (self.api.operations, {"list", "retrieve", "create", "update", "delete"}),
            (self.view.operations, {"create", "update", "delete"}),
        ):
            if len(set(operations)) != len(operations) or set(operations) - allowed:
                raise ImproperlyConfigured("Unknown or duplicate surface operations.")
        if self.api.prefix is not None:
            self._validate_path(self.api.prefix)
        if (
            self.api.enabled
            and self.app_label is None
            and (self.api.prefix or self.slug)
            in {
                "auth",
                "events",
                "tasks",
                "webhooks",
            }
        ):
            raise ImproperlyConfigured("Resource API collection name is reserved.")
        for mapping in (self.form.labels, self.form.help_texts, self.form.widgets):
            if set(mapping) - set(self.form_fields):
                raise ImproperlyConfigured("Form configuration references unpublished fields.")
        self.conditional_writes = (
            type(self.policy).presets is not Policy.presets
            or type(self.policy).write_fields is not Policy.write_fields
        )
        self.read_schema = self.api.get_read_schema(builder=self)
        self.create_schema = self.api.get_create_schema(builder=self)
        self.update_schema = self.api.get_update_schema(builder=self)
        if self.api.enabled and self.pk_name not in self.read_schema.model_fields:
            raise ImproperlyConfigured("API read schemas must declare the primary key.")
        for compiled, schema_base in (
            (self.read_schema, ApiResponse),
            (self.create_schema, ApiRequest),
            (self.update_schema, ApiRequest),
        ):
            if not issubclass(compiled, schema_base):
                raise ImproperlyConfigured(f"API schema must inherit {schema_base.__name__}.")
            aliases = [field.alias or name for name, field in compiled.model_fields.items()]
            if len(set(aliases)) != len(aliases) or any("_" in alias for alias in aliases):
                raise ImproperlyConfigured("API field aliases must be unique camelCase names.")
            if any(
                name in self.api_names
                and (
                    (field.alias or name) != self.api_names[name]
                    or field.validation_alias not in (None, self.api_names[name])
                    or field.serialization_alias not in (None, self.api_names[name])
                )
                for name, field in compiled.model_fields.items()
            ):
                raise ImproperlyConfigured(
                    "API schemas must use the resource's public field names."
                )
            compiled.model_json_schema()

    @staticmethod
    def _validate_path(value: str) -> None:
        if not re.fullmatch(r"[a-z][a-zA-Z0-9]*", value):
            raise ImproperlyConfigured(f"Invalid resource path: {value!r}.")

    def _form_fields(self) -> tuple[str, ...]:
        fieldsets = self.form.fieldsets
        if fieldsets is None:
            return self._projection(self.form.fields)
        if not isinstance(fieldsets, (list, tuple)):
            raise ImproperlyConfigured("Form.fieldsets must be a list or tuple.")
        if not fieldsets:
            return self._projection(self.form.fields)
        if self.form.fields is not None:
            raise ImproperlyConfigured("Both 'fieldsets' and 'fields' are specified.")
        names: list[str] = []
        for index, fieldset in enumerate(fieldsets):
            label = f"Form.fieldsets[{index}]"
            if not isinstance(fieldset, (list, tuple)) or len(fieldset) != 2:
                raise ImproperlyConfigured(f"{label} must be a (title, options) pair.")
            title, options = fieldset
            if title is not None and not isinstance(title, str):
                raise ImproperlyConfigured(f"{label} title must be a string or None.")
            if not isinstance(options, Mapping):
                raise ImproperlyConfigured(f"{label} options must be a mapping.")
            unsupported = set(options) - {"fields", "description"}
            if unsupported:
                raise ImproperlyConfigured(
                    f"Unsupported fieldset option: {sorted(unsupported)[0]!r}."
                )
            if "fields" not in options:
                raise ImproperlyConfigured(f"{label} must contain 'fields'.")
            fields = options["fields"]
            if isinstance(fields, str) or not isinstance(fields, (list, tuple)):
                raise ImproperlyConfigured(f"{label}['fields'] must be a list or tuple.")
            description = options.get("description", "")
            if not isinstance(description, str):
                raise ImproperlyConfigured(f"{label} description must be a string.")
            for name in fields:
                if not isinstance(name, str):
                    raise ImproperlyConfigured("Nested field rows are not supported in fieldsets.")
                if name in names:
                    raise ImproperlyConfigured(f"Duplicate field in fieldsets: {name!r}.")
                names.append(name)
        projected = self._projection(names)
        if len(projected) != len(names):
            raise ImproperlyConfigured("Form fieldsets reference unpublished fields.")
        return projected

    def _projection(self, refs: FieldRefs | None) -> tuple[str, ...]:
        if self.fields is None or self.model is None:
            raise ImproperlyConfigured("Register the builder before using field projections.")
        selected = self.fields if refs is None else refs
        names = normalize(selected, self.model, self.specs)
        return tuple(name for name in names if name in self.fields)

    def _list_projection(self, refs: FieldRefs | None) -> tuple[str, ...]:
        if refs is None:
            return self._projection(None)
        if isinstance(refs, str):
            raise ImproperlyConfigured("Field projections must be sequences, not strings.")
        if self.model is None or self.fields is None:
            raise ImproperlyConfigured("Register the builder before using field projections.")
        result = []
        for ref in refs:
            name = ref if isinstance(ref, str) else getattr(ref, "key", None)
            if not isinstance(ref, str) and getattr(ref, "class_", None) is not self.model:
                raise ImproperlyConfigured("Mapped field references must belong to this model.")
            if not isinstance(name, str) or name in result:
                raise ImproperlyConfigured(f"Unknown, unsupported or duplicate field: {name!r}.")
            if name in self.specs:
                if name not in self.fields:
                    continue
            elif name in self.list_relations:
                fk = self.list_relation_fields.get(name)
                if fk is None:
                    raise ImproperlyConfigured(
                        f"Relation {name!r} must use a single-column foreign key."
                    )
                if fk not in self.fields:
                    raise ImproperlyConfigured(
                        f"Relation {name!r} requires its foreign key {fk!r} in fields."
                    )
            else:
                raise ImproperlyConfigured(f"Unknown, unsupported or duplicate field: {name!r}.")
            result.append(name)
        return tuple(result)

    def list_display(self, ctx: Context) -> tuple[str, ...]:
        if self.policy is None or self.fields is None:
            raise ImproperlyConfigured("Register the builder before using field projections.")
        readable = set(self.readable(ctx))
        return tuple(
            name
            for name in self.view_list_display
            if (name in self.view_list_relations and self.list_relation_fields[name] in readable)
            or (name not in self.view_list_relations and name in readable)
        )

    def readable(
        self, ctx: Context, *, obj: T | None = None, fields: Sequence[str] | None = None
    ) -> tuple[str, ...]:
        if self.policy is None or self.fields is None:
            raise ImproperlyConfigured("Register the builder before using field projections.")
        allowed = self.policy.read_fields(ctx, obj=obj)
        published = self.fields
        if ctx.source == "api" and self.api.enabled:
            published = tuple(dict.fromkeys((self.pk_name, *published)))
        return tuple(
            key
            for key in (published if fields is None else fields)
            if key in published and (allowed is None or key in allowed)
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
        return sa.select(self.model).options(
            undefer("*"),
            raiseload("*"),
            *(
                selectinload(getattr(self.model, key))
                for key, spec in self.specs.items()
                if "obox_files" in spec.column.info
            ),
        )

    def permission_name(self, action: str) -> str:
        prefix = (
            self.policy.resource
            if isinstance(self.policy, RBAC)
            else f"{self.app_label or 'admin'}.{self.slug}"
        )
        return f"{prefix}.{action}"

    async def validate_create(self, data: dict[str, Any], ctx: Context) -> Mapping[str, Any]:
        return data

    async def validate_update(
        self, instance: T, changes: dict[str, Any], ctx: Context
    ) -> Mapping[str, Any]:
        return changes

    async def before_save(
        self,
        instance: T,
        changes: Mapping[str, Any],
        ctx: Context,
        *,
        created: bool,
    ) -> None:
        pass

    async def before_delete(self, instance: T, ctx: Context) -> None:
        pass

    async def after_commit(self, event: DomainEvent, ctx: Context) -> None:
        pass


__all__ = ["API", "Form", "Inline", "ModelBuilder", "View", "action"]
