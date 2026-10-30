"""Explicit model fields, typed CRUD, and permission-scoped persistence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from functools import cached_property
from types import MappingProxyType
from typing import Any, Generic, Literal, TypeVar, cast

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, create_model
from sqlalchemy.orm import selectinload

from obox_builder.exceptions import ImproperlyConfigured, PermissionDenied
from obox_builder.fields import FieldSpec, QuerySpec, introspect_model
from obox_builder.fields.introspect import FieldConfig
from obox_builder.forms import BoundForm, build_schema
from obox_builder.resources.base import Resource
from obox_builder.resources.context import OperationContext
from obox_builder.resources.errors import ResourceValidationError
from obox_builder.resources.operations import Capability, Operation, OperationSpec, operation
from obox_builder.resources.relations import Related
from obox_builder.security import PermissionRef, lower_camel
from obox_builder.utils.strings import camel_to_snake

T = TypeVar("T")


class QueryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filter: dict[str, Any] | None = None
    q: str = ""
    sort: str | None = None
    page: int = Field(default=1, ge=1)
    size: int = Field(default=50, ge=1, le=200)
    total: Literal["none", "capped", "exact"] = "none"


class PageInfo(BaseModel):
    number: int
    size: int
    has_next: bool
    total: int | None = None
    total_capped: bool = False


class Page(BaseModel, Generic[T]):
    items: list[T]
    page: PageInfo


class Batch(BaseModel, Generic[T]):
    items: list[T]


def _builtin(**kwargs: Any) -> Any:
    def decorate(handler: Any) -> Operation:
        return Operation(handler, builtin=True, **kwargs)

    return decorate


class ReadOnlyModelResource(Resource, Generic[T]):
    model: type[T]
    fields: tuple[str, ...] = ()
    fields_config: Mapping[str, FieldConfig] = {}
    readonly_fields: tuple[str, ...] = ()
    ordering: tuple[str, ...] = ()
    search_fields: tuple[str, ...] = ()
    max_selection: int = 200
    max_batch: int = 50
    import_keys: tuple[str, ...] = ()
    related: Mapping[str, Related] = {}

    def __init__(self) -> None:
        super().__init__()
        if not self.related:
            names = ("listRelated", "editRelated")
            methods = {
                value.key
                for base in type(self).__mro__
                for value in vars(base).values()
                if isinstance(value, Operation)
            }
            self.exclude_operations = tuple(
                dict.fromkeys(
                    (*self.exclude_operations, *(name for name in names if name in methods))
                )
            )

    def bind_registry(self, registry: Any) -> None:
        self._related_resources = MappingProxyType(
            {name: registry.get(relation.resource) for name, relation in self.related.items()}
        )
        for name, relation in self.related.items():
            if not name or lower_camel(name) != name:
                raise ImproperlyConfigured("Related collection names must use lowerCamelCase.")
            child = self._related_resources[name]
            if not isinstance(child, ReadOnlyModelResource):
                raise ImproperlyConfigured("Related collections need model resources.")
            relation.validate(self, child)

    def freeze(self) -> None:
        for name in (
            "model",
            "fields",
            "fields_config",
            "readonly_fields",
            "ordering",
            "search_fields",
            "max_selection",
            "max_batch",
            "import_keys",
        ):
            object.__setattr__(self, name, getattr(self, name))
        self.related = MappingProxyType(dict(self.related))
        super().freeze()

    @property
    def scope_name(self) -> str:
        return f"{self.model._model_label}.{camel_to_snake(self.model.__name__)}"  # type: ignore[attr-defined]

    @cached_property
    def specs(self) -> Mapping[str, FieldSpec]:
        if not isinstance(self.fields, tuple) or not self.fields:
            raise ImproperlyConfigured(f"{self.name}.fields must be an explicit nonempty tuple.")
        all_specs = {
            spec.name: spec for spec in introspect_model(self.model, overrides=self.fields_config)
        }
        if len(set(self.fields)) != len(self.fields) or set(self.fields) - all_specs.keys():
            raise ImproperlyConfigured(f"{self.name}.fields contains duplicate or unknown names.")
        if set(self.readonly_fields) - set(self.fields):
            raise ImproperlyConfigured(f"{self.name}.readonly_fields contains unpublished fields.")
        result: dict[str, FieldSpec] = {}
        for name in self.fields:
            spec = all_specs[name]
            if spec.secret:
                raise ImproperlyConfigured(f"Secret field {self.name}.{name} cannot be published.")
            if name in self.readonly_fields:
                spec = replace(spec, readonly=True, editable=False, required=False)
            options = dict(spec.options)
            for kind in ("read_permission", "write_permission"):
                reference = options.get(kind)
                if isinstance(reference, Capability):
                    reference = reference.bind(self.name)
                if reference is not None and not isinstance(reference, PermissionRef):
                    raise ImproperlyConfigured(
                        f"{self.name}.{name}.{kind} needs a permission reference."
                    )
                options[kind] = reference
            result[name] = replace(spec, options=MappingProxyType(options))
        return MappingProxyType(result)

    def validate(self) -> None:
        _ = self.specs
        self.pk_column()
        if set(self.fields_config) - self.specs.keys():
            raise ImproperlyConfigured(f"{self.name}.fields_config contains unpublished fields.")
        if "create" in self.definitions and self.pk_column().key not in self.fields:
            raise ImproperlyConfigured("Writable model resources must publish their primary key.")
        super().validate()
        for spec in self.specs.values():
            for key in ("read_permission", "write_permission"):
                ref = spec.options.get(key)
                if (
                    ref is not None
                    and ref.name.startswith(self.name + ".")
                    and ref.name not in self.permissions
                ):
                    raise ImproperlyConfigured(f"Unknown field permission {ref.name!r}.")

    def pk_column(self) -> Any:
        mapper: Any = sa.inspect(self.model)
        columns = mapper.primary_key
        if len(columns) != 1:
            raise ImproperlyConfigured("Model resources require one primary key column.")
        return columns[0]

    def get_pk(self, obj: T) -> Any:
        return getattr(obj, self.pk_column().key)

    def parse_pk(self, value: Any) -> Any:
        try:
            return self.pk_column().type.python_type(value)
        except (ValueError, TypeError) as exc:
            raise LookupError("Record is unavailable.") from exc

    def get_queryset(self, ctx: OperationContext) -> Any:
        stmt = sa.select(self.model)
        for spec in self.specs.values():
            relationship = spec.options.get("relationship")
            if relationship:
                stmt = stmt.options(selectinload(getattr(self.model, relationship)))
        return stmt

    def scoped_query(self, ctx: OperationContext, spec: OperationSpec) -> Any:
        return self.policy.scope(ctx, self.get_queryset(ctx), spec)

    async def resolve_target(self, ctx: OperationContext, spec: OperationSpec, target: Any) -> Any:
        if target is None:
            raise ResourceValidationError({"target": ["A target is required."]})
        targets = list(target) if spec.scope == "selection" else [target]
        if not targets or len(targets) > self.max_selection:
            raise ResourceValidationError({"target": ["Invalid selection size."]})
        ids = [
            self.get_pk(item) if isinstance(item, self.model) else self.parse_pk(item)
            for item in targets
        ]
        if len(set(ids)) != len(ids):
            raise ResourceValidationError({"target": ["Duplicate record identifiers."]})
        stmt = (
            self.scoped_query(ctx, spec).where(self.pk_column().in_(ids)).order_by(self.pk_column())
        )
        if spec.effect != "read":
            stmt = stmt.with_for_update(of=self.model).execution_options(populate_existing=True)
        rows = list((await ctx.session.scalars(stmt)).unique())
        if len(rows) != len(ids):
            raise LookupError("A selected record is unavailable.")
        return rows if spec.scope == "selection" else rows[0]

    def field_allowed(self, ctx: OperationContext, spec: FieldSpec, *, write: bool = False) -> bool:
        reference = spec.options.get("write_permission" if write else "read_permission")
        return reference is None or ctx.can(reference, effect="write" if write else "read")

    def query_specs(self, ctx: OperationContext) -> dict[str, FieldSpec]:
        return {name: spec for name, spec in self.specs.items() if self.field_allowed(ctx, spec)}

    @cached_property
    def read_schema(self) -> type[BaseModel]:
        fields: dict[str, Any] = {}
        for spec in self.specs.values():
            annotation = spec.type.output_annotation(spec)
            if (
                spec.primary_key
                or spec.type.name in {"link", "decimal", "uuid"}
                or (spec.column is not None and isinstance(spec.column.type, sa.BigInteger))
            ):
                annotation = str
            elif spec.type.name == "multi_link":
                annotation = list[str]
            nullable = (
                spec.nullable
                or spec.type.name == "link"
                or spec.options.get("read_permission") is not None
                or spec.show_if is not None
            )
            fields[spec.name] = (
                annotation | None if nullable else annotation,
                None if nullable else ...,
            )
        return create_model(
            self.name.replace(".", "_") + "Read", __config__=ConfigDict(extra="forbid"), **fields
        )

    @cached_property
    def create_schema(self) -> type[BaseModel]:
        return build_schema(
            list(self.specs.values()), name=self.name.replace(".", "_"), mode="create"
        )

    @cached_property
    def update_schema(self) -> type[BaseModel]:
        fields: dict[str, Any] = {
            key: (value.annotation, None) for key, value in self.create_schema.model_fields.items()
        }
        return create_model(
            self.name.replace(".", "_") + "Update",
            __config__=ConfigDict(extra="forbid"),
            **fields,
        )

    def builtin_schemas(self, method: str) -> tuple[type[BaseModel] | None, type[BaseModel] | None]:
        if method in {"list_related", "edit_related"}:
            from obox_builder.resources.relations import schemas as related_schemas

            return related_schemas(self, method)
        schemas: dict[str, tuple[type[BaseModel] | None, type[BaseModel] | None]] = {
            "list": (QueryInput, self.page_schema),
            "get": (None, self.read_schema),
            "create": (self.create_schema, self.read_schema),
            "update": (self.update_schema, self.read_schema),
            "delete": (None, None),
        }
        return schemas[method]

    @cached_property
    def page_schema(self) -> type[BaseModel]:
        return cast(type[BaseModel], Page.__class_getitem__(self.read_schema))

    async def serialize(self, ctx: OperationContext, obj: T) -> BaseModel:
        from obox_builder.resources.validation import relation_rows

        state: Any = sa.inspect(obj)
        attributes = [
            str(spec.column.key)
            if spec.type.name == "link" and spec.column is not None
            else spec.name
            for spec in self.specs.values()
            if self.field_allowed(ctx, spec)
        ]
        unloaded = set(attributes) & state.unloaded
        if unloaded:
            await ctx.session.refresh(obj, list(unloaded))
        values: dict[str, Any] = {}
        for spec in self.specs.values():
            if not self.field_allowed(ctx, spec):
                continue
            if spec.show_if is not None and not spec.show_if(ctx.record_context(obj)):
                continue
            if spec.type.name == "multi_link":
                target = spec.options["target"]
                key = sa.inspect(target).primary_key[0].key
                ids = [getattr(row, key) for row in getattr(obj, spec.name)]
                visible = {getattr(row, key) for row in await relation_rows(ctx, spec, ids)}
                values[spec.name] = [str(value) for value in ids if value in visible]
            else:
                attr = (
                    spec.column.key
                    if spec.type.name == "link" and spec.column is not None
                    else spec.name
                )
                value = getattr(obj, str(attr))
                if (
                    spec.type.name == "link"
                    and value is not None
                    and not await relation_rows(ctx, spec, [value])
                ):
                    value = None
                values[spec.name] = spec.type.to_api(value, spec)
        return self.read_schema.model_validate(values)

    def record_label(self, ctx: OperationContext, obj: T) -> str:
        for spec in self.specs.values():
            if spec.name in {"name", "title", "reference", "username"} and self.field_allowed(
                ctx, spec
            ):
                return str(getattr(obj, spec.name))
        return f"#{self.get_pk(obj)}"

    async def record_visible(self, ctx: OperationContext, obj: T) -> bool:
        return self.can_execute(ctx, "list", obj)

    @_builtin(permission="read", scope="collection", effect="read")
    async def list(self, ctx: OperationContext, data: QueryInput) -> Any:
        import json

        params = data.model_dump(exclude_none=True)
        if data.filter is not None:
            params["filter"] = json.dumps(data.filter)
        specs = self.query_specs(ctx)
        query = QuerySpec.parse(params, specs, default_sort=self.ordering)
        stmt = query.apply(
            self.scoped_query(ctx, self.operations["list"]),
            specs,
            search_specs=[specs[name] for name in self.search_fields if name in specs],
        )
        count = None
        capped = False
        if data.total != "none":
            counted = stmt.order_by(None).with_only_columns(self.pk_column())
            if data.total == "capped":
                counted = counted.limit(10_001)
            count = int(
                await ctx.session.scalar(sa.select(sa.func.count()).select_from(counted.subquery()))
                or 0
            )
            capped = data.total == "capped" and count > 10_000
            count = min(count, 10_000) if capped else count
        stmt = (
            stmt.order_by(self.pk_column()).offset((data.page - 1) * data.size).limit(data.size + 1)
        )
        rows = list((await ctx.session.scalars(stmt)).unique())
        items = [
            await self.serialize(ctx, obj)
            for obj in rows[: data.size]
            if await self.record_visible(ctx, obj)
        ]
        return self.page_schema(
            items=items,
            page=PageInfo(
                number=data.page,
                size=data.size,
                has_next=len(rows) > data.size,
                total=count,
                total_capped=capped,
            ),
        )

    @_builtin(permission="read", scope="record", effect="read")
    async def get(self, ctx: OperationContext, obj: T) -> Any:
        return await self.serialize(ctx, obj)

    @_builtin(permission="read", scope="record", effect="read")
    async def list_related(self, ctx: OperationContext, obj: T, data: Any) -> Any:
        from obox_builder.resources.relations import snapshot

        return await snapshot(self, ctx, obj, data.root.relation)


class ModelResource(ReadOnlyModelResource[T]):
    def __init__(self) -> None:
        super().__init__()
        if not self.import_keys:
            self.exclude_operations = tuple(dict.fromkeys((*self.exclude_operations, "upsert")))

    def validate(self) -> None:
        if self.import_keys:
            from obox_builder.resources.upsert import validate_keys

            validate_keys(self)
        super().validate()

    @_builtin(permission="update", scope="collection", effect="write")
    async def upsert(self, ctx: OperationContext, data: Any) -> Any:
        from obox_builder.resources.upsert import execute

        return await execute(self, ctx, data)

    @_builtin(permission="update", scope="record", effect="write")
    async def edit_related(self, ctx: OperationContext, obj: T, data: Any) -> Any:
        from obox_builder.resources.relations import edit

        return await edit(self, ctx, obj, data.root)

    @cached_property
    def bulk_create_schema(self) -> type[BaseModel]:
        return create_model(
            self.name.replace(".", "_") + "BulkCreate",
            __config__=ConfigDict(extra="forbid"),
            items=(
                cast(Any, list.__class_getitem__(self.create_schema)),
                Field(min_length=1, max_length=self.max_batch),
            ),
        )

    @cached_property
    def bulk_update_schema(self) -> type[BaseModel]:
        change = create_model(
            self.name.replace(".", "_") + "Change",
            __config__=ConfigDict(extra="forbid"),
            id=(str, ...),
            data=(self.update_schema, ...),
        )
        return create_model(
            self.name.replace(".", "_") + "BulkUpdate",
            __config__=ConfigDict(extra="forbid"),
            changes=(
                cast(Any, list.__class_getitem__(change)),
                Field(min_length=1, max_length=self.max_batch),
            ),
        )

    @cached_property
    def batch_schema(self) -> type[BaseModel]:
        return cast(type[BaseModel], Batch.__class_getitem__(self.read_schema))

    def builtin_schemas(self, method: str) -> tuple[type[BaseModel] | None, type[BaseModel] | None]:
        if method == "upsert":
            from obox_builder.resources.upsert import schema

            return schema(self), self.batch_schema
        if method == "bulk_create":
            return self.bulk_create_schema, self.batch_schema
        if method == "bulk_update":
            return self.bulk_update_schema, self.batch_schema
        return super().builtin_schemas(method)

    @_builtin(scope="collection", effect="write", permission="create")
    async def bulk_create(self, ctx: OperationContext, data: Any) -> Any:
        rows = data.items
        for row in rows:
            await self.bind(ctx, row.model_dump(exclude_unset=True))
        async with ctx.session.begin_nested():
            results = [await self.execute("create", ctx, row) for row in rows]
        return self.batch_schema(items=results)

    @_builtin(scope="selection", effect="write", permission="update")
    async def bulk_update(self, ctx: OperationContext, objects: list[T], data: Any) -> Any:
        changes = data.changes
        keys = [self.parse_pk(change.id) for change in changes]
        if len(set(keys)) != len(keys) or set(keys) != {self.get_pk(obj) for obj in objects}:
            raise ResourceValidationError(
                {"changes": ["Changes must match the selected records exactly."]}
            )
        targets = {self.get_pk(obj): obj for obj in objects}
        for change, key in zip(changes, keys, strict=True):
            obj = targets[key]
            if not self.can_execute(ctx, "update", obj):
                raise PermissionDenied("A selected record cannot be updated.")
            await self.bind(ctx, change.data.model_dump(exclude_unset=True), obj)
        async with ctx.session.begin_nested():
            results = [
                await self.execute("update", ctx, change.data, target=targets[key])
                for change, key in zip(changes, keys, strict=True)
            ]
        return self.batch_schema(items=results)

    async def bind(
        self, ctx: OperationContext, data: Mapping[str, Any], obj: T | None = None
    ) -> BoundForm:
        errors: dict[str, list[str]] = {}
        base = ctx.record_context(obj, data)
        for name in data:
            spec = self.specs.get(name)
            if (
                spec is None
                or not spec.is_editable()
                or not self.field_allowed(ctx, spec, write=True)
                or spec.editable_if is not None
                and not spec.editable_if(base)
            ):
                errors[name] = ["This field cannot be written."]
        if errors:
            raise ResourceValidationError(errors)
        bound = BoundForm.bind(
            list(self.specs.values()),
            self.create_schema,
            data,
            base=base,
            partial=obj is not None,
            api=True,
        )
        if bound.is_valid:
            from obox_builder.resources.validation import validate_relations, validate_unique

            await validate_relations(self, ctx, bound)
            await validate_unique(self.model, ctx, bound, obj)
        if not bound.is_valid:
            raise ResourceValidationError(bound.errors)
        return bound

    async def save_model(
        self, ctx: OperationContext, obj: T, form: BoundForm, *, change: bool
    ) -> None:
        ctx.session.add(obj)
        await ctx.session.flush()

    async def save_related(
        self, ctx: OperationContext, obj: T, form: BoundForm, *, change: bool
    ) -> None:
        from obox_builder.resources.validation import save_relations

        await save_relations(self, ctx, obj, form)

    async def delete_model(self, ctx: OperationContext, obj: T) -> None:
        await ctx.session.delete(obj)

    @_builtin(scope="collection", effect="write")
    async def create(self, ctx: OperationContext, data: BaseModel) -> Any:
        bound = await self.bind(ctx, data.model_dump(exclude_unset=True))
        obj = self.model()
        ctx = replace(ctx, obj=obj)
        async with ctx.session.begin_nested():
            bound.apply_to(obj)
            if not self.can_execute(ctx, "create", obj):
                raise PermissionDenied("This record cannot be created.")
            await self.save_model(ctx, obj, bound, change=False)
            await self.save_related(ctx, obj, bound, change=False)
            await ctx.session.flush()
        return await self.serialize(ctx, obj)

    @_builtin(scope="record", effect="write")
    async def update(self, ctx: OperationContext, obj: T, data: BaseModel) -> Any:
        bound = await self.bind(ctx, data.model_dump(exclude_unset=True), obj)
        async with ctx.session.begin_nested():
            bound.apply_to(obj)
            await self.save_model(ctx, obj, bound, change=True)
            await self.save_related(ctx, obj, bound, change=True)
            await ctx.session.flush()
        return await self.serialize(ctx, obj)

    @_builtin(scope="record", effect="delete")
    async def delete(self, ctx: OperationContext, obj: T) -> None:
        from obox_builder.resources.deletion import collect_deletion
        from obox_builder.resources.errors import ResourceConflict

        plan = await collect_deletion(ctx, [obj], lock=True)
        if plan.permissions:
            raise PermissionDenied("Related records cannot be deleted with these permissions.")
        if plan.blockers:
            raise ResourceConflict("; ".join(plan.blockers))
        await self.delete_model(ctx, obj)
        await ctx.session.flush()

    @operation(scope="selection", effect="delete", permission="delete")
    async def delete_selected(self, ctx: OperationContext, objects: list[T]) -> None:
        from obox_builder.resources.deletion import collect_deletion
        from obox_builder.resources.errors import ResourceConflict

        plan = await collect_deletion(ctx, objects, lock=True)
        if plan.permissions:
            raise PermissionDenied("Related records cannot be deleted with these permissions.")
        if plan.blockers:
            raise ResourceConflict("; ".join(plan.blockers))
        async with ctx.session.begin_nested():
            for obj in objects:
                await self.delete_model(replace(ctx, obj=obj), obj)
            await ctx.session.flush()
