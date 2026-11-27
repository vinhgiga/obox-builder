"""Resource-backed model forms and bounded inline edits."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Generic, cast

import sqlalchemy as sa
from sqlalchemy.orm import MANYTOMANY, MANYTOONE, NO_VALUE, attributes, object_mapper, selectinload
from sqlalchemy.orm.util import object_state
from typing_extensions import TypeVar

from obox.core.exceptions import BuilderValidationError, ImproperlyConfigured
from obox.core.principal import ANONYMOUS
from obox.db.session import session_scope
from obox.forms.fields import Field, JSONField
from obox.forms.forms import Form, S
from obox.forms.relations import (
    ModelChoiceField,
    ModelMultipleChoiceField,
    save_model_relations,
    validate_model_constraints,
)
from obox.resources import ModelResource

ModelT = TypeVar("ModelT", default=Any)


def _relationship_field(relation: Any) -> Field:
    if "obox_files" in relation.info:
        from obox.contrib.files.forms import AttachmentField

        return AttachmentField(relation.parent.class_, relation.key, relation.info["obox_files"])
    if relation.viewonly or len(relation.synchronize_pairs) != 1:
        raise ImproperlyConfigured(f"Unsupported form relationship: {relation.key!r}.")
    if relation.direction is MANYTOMANY:
        linked = {
            pair[1] for pair in (*relation.synchronize_pairs, *relation.secondary_synchronize_pairs)
        }
        if relation.collection_class not in (None, list) or any(
            column not in linked
            and not column.nullable
            and column.default is None
            and column.server_default is None
            and not (column.primary_key and column.autoincrement is True)
            for column in relation.secondary.c
        ):
            raise ImproperlyConfigured("Use a form for the association model.")
        return ModelMultipleChoiceField(sa.select(relation.mapper.class_), required=False)
    if relation.direction is not MANYTOONE:
        raise ImproperlyConfigured(f"Unsupported form relationship: {relation.key!r}.")
    column = relation.synchronize_pairs[0][1]
    return ModelChoiceField(
        sa.select(relation.mapper.class_),
        required=not column.nullable and column.default is None and column.server_default is None,
    )


class ModelForm(Form[S], Generic[S, ModelT]):
    resource: ModelResource[ModelT] | type[ModelResource[ModelT]] | None = None
    instance: ModelT

    def __init_subclass__(cls, **kwargs: Any) -> None:
        if isinstance(cls.resource, type):
            cls.resource = cls.resource()
        if cls.resource is not None:
            cls.schema = cls.resource.get_schema("create")
        super().__init_subclass__(**kwargs)

    def __init__(
        self,
        data: Any = None,
        files: Any = None,
        *,
        instance: ModelT | None = None,
        initial: Mapping[str, Any] | None = None,
        prefix: str | None = None,
        empty_permitted: bool = False,
        resource: ModelResource[ModelT] | None = None,
    ) -> None:
        definition = resource or self.resource
        if not isinstance(definition, ModelResource):
            raise ImproperlyConfigured("Define a ModelResource on the form.")
        self.resource = definition
        self.instance = definition.model() if instance is None else instance
        state = object_state(self.instance)
        action = "create" if state.transient else "update"
        schema = definition.get_schema(action)
        values = {
            name: attr.loaded_value
            for name, attr in state.attrs.items()
            if attr.loaded_value is not NO_VALUE
        }
        if instance is None:
            values = {
                name: spec.column.default.arg
                for name, spec in definition.specs.items()
                if spec.column.default is not None and spec.column.default.is_scalar
            }
        super().__init__(
            data,
            files,
            schema=cast(type[S], schema),
            initial={**values, **(initial or {})},
            prefix=prefix,
            empty_permitted=empty_permitted,
        )
        mapper = state.mapper
        for relation in mapper.relationships:
            name = relation.key
            if "obox_files" in relation.info and name in self.fields:
                self.fields[name] = _relationship_field(relation)
            if name in self.fields and relation.direction is MANYTOONE:
                for _, local in relation.synchronize_pairs:
                    local_name = mapper.get_property_by_column(local).key
                    if local_name in self.field_options:
                        raise ImproperlyConfigured(
                            "Choose a relationship or its foreign key, not both."
                        )
                    self.fields.pop(local_name, None)
        for name, field in self.fields.items():
            if hasattr(field, "owner_model"):
                cast(Any, field).instance = self.instance
            if (
                name in definition.specs
                and isinstance(definition.specs[name].column.type, sa.JSON)
                and not hasattr(field, "queryset")
            ):
                self.fields[name] = JSONField(required=field.required, widget=field.widget)

    async def prepare(self) -> None:
        state = object_state(self.instance)
        missing = [
            name
            for name in self.fields
            if name in state.mapper.relationships and name not in self.initial
        ]
        if state.identity is not None and missing:
            async with session_scope() as session:
                with session.no_autoflush:
                    obj = await session.scalar(
                        sa.select(type(self.instance))
                        .where(
                            *(
                                column == value
                                for column, value in zip(
                                    state.mapper.primary_key, state.identity, strict=True
                                )
                            )
                        )
                        .options(
                            *(selectinload(getattr(type(self.instance), name)) for name in missing)
                        )
                    )
                    if obj is not None:
                        self.initial.update({name: getattr(obj, name) for name in missing})
        await super().prepare()

    async def full_clean(self) -> None:
        if not self.is_bound:
            await super().full_clean()
            return
        async with session_scope() as session:
            with session.no_autoflush:
                if any(field.disabled for field in self.fields.values()):
                    await self.prepare()
                await super().full_clean()

    def prepare_data(self, data: dict[str, Any], resolved: dict[str, Any]) -> dict[str, Any]:
        mapper = object_mapper(self.instance)
        for name, value in resolved.items():
            if name in mapper.relationships:
                relation = mapper.relationships[name]
                if relation.direction is MANYTOONE:
                    for remote, local in relation.synchronize_pairs:
                        data[mapper.get_property_by_column(local).key] = (
                            getattr(value, relation.mapper.get_property_by_column(remote).key)
                            if value is not None
                            else None
                        )
        return data

    def _column_data(self) -> tuple[dict[str, Any], dict[str, str]]:
        mapper = object_mapper(self.instance)
        sources = {name: name for name in self.fields if name in mapper.column_attrs}
        for name in self.fields.keys() & mapper.relationships.keys():
            relation = mapper.relationships[name]
            if relation.direction is MANYTOONE:
                sources.update(
                    {
                        mapper.get_property_by_column(local).key: name
                        for _, local in relation.synchronize_pairs
                    }
                )
        return {
            name: self.validated_data[name] for name in sources if name in self.validated_data
        }, sources

    async def post_clean(self) -> None:
        values, sources = self._column_data()
        async with session_scope() as session:
            try:
                await validate_model_constraints(
                    session, type(self.instance), values, self.instance, unique=True
                )
            except BuilderValidationError as exc:
                for name, messages in exc.errors.items():
                    target = sources.get(name, name)
                    self.add_error(target if target in self.fields else None, messages)

    async def save(self) -> ModelT:
        if not await self.is_valid():
            raise ValueError("The form cannot be saved because its data did not validate.")
        async with session_scope() as session:
            ctx = session.info.get("obox.runtime.context")
            definition = cast(ModelResource[ModelT], self.resource)
            resource = definition.bind(session, ctx.principal if ctx else ANONYMOUS)
            values, sources = self._column_data()
            for name in self.fields.keys() & resource.specs.keys():
                if "obox_files" in resource.specs[name].column.info:
                    values[name] = getattr(self.cleaned_data, name)
                    sources[name] = name
            values = {
                name: value
                for name, value in values.items()
                if not self.fields[sources[name]].disabled
                and name in resource.writable
                and not (
                    self.add_prefix(sources[name]) not in self.data
                    and (
                        resource.specs[name].column.default is not None
                        or resource.specs[name].column.server_default is not None
                    )
                    and not isinstance(resource.specs[name].column.type, sa.Boolean)
                )
            }
            original = self.instance
            self.instance = (
                await resource.create(values, validate=False, instance=original)
                if object_state(original).transient
                else await resource.update(original, values, validate=False)
            )
            relations = {
                name: value
                for name, value in self.validated_data.items()
                if name in self.fields
                and name in object_mapper(self.instance).relationships
                and not self.fields[name].disabled
                and "obox_files" not in object_mapper(self.instance).relationships[name].info
            }
            if relations:
                await save_model_relations(session, self.instance, relations)
            if original is not self.instance and object_state(original).identity is not None:
                state = object_state(self.instance)
                for name in set(state.mapper.attrs.keys()) - state.unloaded:
                    attributes.set_committed_value(original, name, getattr(self.instance, name))
                self.instance = original
        return self.instance


def modelform_factory(
    resource: type[ModelResource[ModelT]] | ModelResource[ModelT],
    *,
    form: type[ModelForm[S, ModelT]] = ModelForm,
) -> type[ModelForm[S, ModelT]]:
    definition = resource() if isinstance(resource, type) else resource
    if not isinstance(definition, ModelResource):
        raise ImproperlyConfigured("Provide a ModelResource.")
    return type(definition.model.__name__ + "Form", (form,), {"resource": definition})
