"""Resource-backed model forms and bounded inline edits."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Generic, cast

import sqlalchemy as sa
from sqlalchemy.orm import MANYTOMANY, MANYTOONE, NO_VALUE, attributes, object_mapper, selectinload
from sqlalchemy.orm.util import object_state
from typing_extensions import TypeVar

from obox.core.exceptions import BuilderValidationError, ImproperlyConfigured, ValidationError
from obox.core.principal import ANONYMOUS
from obox.db.query import scope_query
from obox.db.session import session_scope
from obox.forms.fields import Field, JSONField
from obox.forms.forms import Form, S
from obox.forms.formsets import BaseFormSet, formset_factory
from obox.forms.relations import (
    ModelChoiceField,
    ModelMultipleChoiceField,
    save_model_relations,
    validate_model_constraints,
)
from obox.forms.widgets import HiddenInput
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
                else await resource.update(original, values, partial=True, validate=False)
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


class _IdentityField(Field):
    widget = HiddenInput

    def __init__(self, objects: dict[str, Any], key: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.objects, self.key = objects, key

    def to_python(self, value: Any) -> Any:
        if value in self.empty_values:
            return None
        obj = self.objects.get(str(value))
        if obj is None:
            raise ValidationError(
                "Select a valid choice. That choice is not one of the available choices."
            )
        return getattr(obj, self.key)


class InlineForeignKeyField(Field):
    widget = HiddenInput

    def __init__(self, parent_instance: Any, *, to_field: str, relationship: bool = False) -> None:
        super().__init__(required=False, initial=getattr(parent_instance, to_field, None))
        self.parent_instance, self.to_field, self.relationship = (
            parent_instance,
            to_field,
            relationship,
        )

    def to_python(self, value: Any) -> Any:
        expected = getattr(self.parent_instance, self.to_field, None)
        if value not in self.empty_values and str(value) != str(expected):
            raise ValidationError(
                "The inline value did not match the parent instance.", code="invalid_choice"
            )
        return self.parent_instance if self.relationship else expected

    def has_changed(self, initial: Any, data: Any) -> bool:
        return False


class BaseModelFormSet(BaseFormSet):
    model: type[Any]

    def __init__(
        self, data: Any = None, files: Any = None, *, queryset: Any = None, **kwargs: Any
    ) -> None:
        self.queryset = sa.select(self.model) if queryset is None else queryset
        mapper = sa.inspect(self.model)
        if len(mapper.primary_key) != 1:
            raise ImproperlyConfigured("Model formsets require one primary key.")
        self.pk = mapper.primary_key[0]
        self.pk_name = mapper.get_property_by_column(self.pk).key
        self._objects: dict[str, Any] | None = None
        self.changed_objects: list[tuple[Any, list[str]]] = []
        self.deleted_objects: list[Any] = []
        self.new_objects: list[Any] = []
        super().__init__(data, files, **kwargs)

    def get_queryset(self) -> Any:
        return (
            self.queryset.order_by(self.pk)
            if not self.queryset._order_by_clauses
            else self.queryset
        )

    async def prepare(self) -> None:
        if self._objects is None:
            async with session_scope() as session:
                with session.no_autoflush:
                    objects = (await session.scalars(self.get_queryset())).unique().all()
            self._objects = {str(getattr(obj, self.pk_name)): obj for obj in objects}
        await super().prepare()

    def initial_form_count(self) -> int:
        return super().initial_form_count() if self.is_bound else len(self._objects or {})

    def construct_form(self, index: int | None, **kwargs: Any) -> ModelForm[Any]:
        if self._objects is None and index is not None:
            raise ValueError("Await formset.prepare() before accessing model forms.")
        if index is not None and index < self.initial_form_count():
            identity = (
                str(self.data.get(f"{self.add_prefix(index)}-{self.pk_name}", ""))
                if self.is_bound
                else list(self._objects or {})[index]
            )
            kwargs["instance"] = (self._objects or {}).get(identity)
        return cast(ModelForm[Any], super().construct_form(index, **kwargs))

    def add_fields(self, form: Form[Any], index: int | None) -> None:
        super().add_fields(form, index)
        if self.pk_name not in form.fields:
            form.fields[self.pk_name] = _IdentityField(
                self._objects or {},
                self.pk_name,
                required=index is not None and index < self.initial_form_count(),
            )
        if isinstance(form, ModelForm):
            form.initial[self.pk_name] = getattr(form.instance, self.pk_name, None)

    def clean(self) -> None:
        self.validate_unique()

    def validate_unique(self) -> None:
        constraints = [
            tuple(column.key for column in constraint.columns)
            for constraint in sa.inspect(self.model).local_table.constraints
            if isinstance(constraint, (sa.UniqueConstraint, sa.PrimaryKeyConstraint))
        ]
        seen: dict[tuple[str, ...], set[Any]] = {names: set() for names in constraints}
        duplicates = False
        identities: set[Any] = set()
        for form in cast(list[ModelForm[Any]], self.forms):
            if form.errors or self.should_delete_form(form) or not form.validated_data:
                continue
            state = sa.inspect(form.instance)
            if state.identity is not None:
                if state.identity in identities:
                    form.add_error(None, "Please correct the duplicate data below.")
                    duplicates = True
                identities.add(state.identity)
            values, _ = form._column_data()
            for names in constraints:
                if not all(name in values for name in names):
                    continue
                row = tuple(
                    "__parent__" if name in getattr(self, "unique_fields", ()) else values[name]
                    for name in names
                )
                if any(value is None for value in row):
                    continue
                if row in seen[names]:
                    form.add_error(None, "Please correct the duplicate data below.")
                    duplicates = True
                seen[names].add(row)
        if duplicates:
            self.errors = [form.errors for form in self.forms if not self.should_delete_form(form)]
            raise ValidationError("Please correct the duplicate data in the forms.")

    async def save_new(
        self,
        form: ModelForm[Any],
    ) -> Any:
        return await form.save()

    async def save_existing(
        self,
        form: ModelForm[Any],
        instance: Any,
    ) -> Any:
        return await form.save()

    async def delete_existing(self, obj: Any) -> None:
        async with session_scope() as session:
            ctx = session.info.get("obox.runtime.context")
            definition = cast(ModelResource[Any], cast(type[ModelForm[Any]], self.form).resource)
            await definition.bind(session, ctx.principal if ctx else ANONYMOUS).destroy(obj)

    async def save(
        self,
    ) -> list[Any]:
        if not await self.is_valid():
            raise ValueError("The formset could not be saved because the data didn't validate.")
        self.changed_objects, self.deleted_objects, self.new_objects = (
            [],
            [],
            [],
        )

        async def persist() -> list[Any]:
            saved = []
            for form in cast(list[ModelForm[Any]], self.forms):
                existing = sa.inspect(form.instance).identity is not None
                if self.should_delete_form(form):
                    if existing:
                        self.deleted_objects.append(form.instance)
                        await self.delete_existing(form.instance)
                    continue
                if not form.has_changed():
                    continue
                if existing:
                    saved.append(await self.save_existing(form, form.instance))
                    self.changed_objects.append((form.instance, form.changed_data))
                else:
                    saved.append(await self.save_new(form))
                    self.new_objects.append(form.instance)
            return saved

        async with session_scope():
            return await persist()


class BaseInlineFormSet(BaseModelFormSet):
    parent_model: type[Any]
    fk: Any
    fk_name: str
    to_field: str
    relation_name: str | None = None
    unique_fields: set[str]

    def __init__(
        self,
        data: Any = None,
        files: Any = None,
        instance: Any = None,
        *,
        queryset: Any = None,
        **kwargs: Any,
    ) -> None:
        self.instance = self.parent_model() if instance is None else instance
        query = sa.select(self.model) if queryset is None else scope_query(queryset, self.model)
        value = getattr(self.instance, self.to_field, None)
        query = query.where(self.fk == value) if value is not None else query.where(sa.false())
        self.unique_fields = {self.fk_name}
        super().__init__(data, files, queryset=query, **kwargs)

    @classmethod
    def get_default_prefix(cls) -> str:
        return cls.relation_name or cls.model.__name__.lower() + "_set"

    def add_fields(self, form: Form[Any], index: int | None) -> None:
        super().add_fields(form, index)
        mapper = sa.inspect(self.model)
        name = next(
            (
                name
                for name in form.fields
                if name in mapper.relationships
                and any(
                    local is self.fk for _, local in mapper.relationships[name].synchronize_pairs
                )
            ),
            self.fk_name,
        )
        form.fields[name] = InlineForeignKeyField(
            self.instance,
            to_field=self.to_field,
            relationship=name in mapper.relationships,
        )
        form.initial[name] = getattr(self.instance, self.to_field, None)

    async def save_new(
        self,
        form: ModelForm[Any],
    ) -> Any:
        value = getattr(self.instance, self.to_field, None)
        if value is None:
            raise ValueError("Save the parent instance before saving its inline forms.")
        setattr(form.instance, self.fk_name, value)
        for name, field in form.fields.items():
            if isinstance(field, InlineForeignKeyField):
                form.validated_data[name] = self.instance if field.relationship else value
        return await super().save_new(form)


def modelformset_factory(
    resource: type[ModelResource[Any]] | ModelResource[Any],
    *,
    form: type[ModelForm[Any]] = ModelForm,
    formset: type[BaseModelFormSet] = BaseModelFormSet,
    **kwargs: Any,
) -> type[BaseModelFormSet]:
    cls = modelform_factory(resource, form=form)
    result = cast(type[BaseModelFormSet], formset_factory(cls, formset, **kwargs))
    result.model = cast(ModelResource[Any], cls.resource).model
    return result


def _get_foreign_key(parent_model: type[Any], model: type[Any], fk_name: str | None = None) -> Any:
    mapper, parent = sa.inspect(model), sa.inspect(parent_model)
    candidates = [
        (prop.key, prop.columns[0], fk.column)
        for prop in mapper.column_attrs
        for fk in prop.columns[0].foreign_keys
        if fk.column.table in parent.tables
    ]
    if fk_name in mapper.relationships:
        relation = mapper.relationships[fk_name]
        candidates = [
            item
            for item in candidates
            if any(local is item[1] for _, local in relation.synchronize_pairs)
        ]
    elif fk_name is not None:
        candidates = [item for item in candidates if item[0] == fk_name]
    if len(candidates) != 1 or len(next(iter(candidates))[1].foreign_keys) != 1:
        raise ValueError(
            "The inline model must have one foreign key to its parent; "
            "specify fk_name if ambiguous."
        )
    name, column, remote = candidates[0]
    if any(len(key.constraint.elements) != 1 for key in column.foreign_keys):
        raise ValueError("Composite inline foreign keys are not supported.")
    return name, column, parent.get_property_by_column(remote).key


def inlineformset_factory(
    parent_model: type[Any],
    resource: type[ModelResource[Any]] | ModelResource[Any],
    *,
    form: type[ModelForm[Any]] = ModelForm,
    formset: type[BaseInlineFormSet] = BaseInlineFormSet,
    fk_name: str | None = None,
    extra: int = 3,
    can_delete: bool = True,
    **kwargs: Any,
) -> type[BaseInlineFormSet]:
    definition = resource() if isinstance(resource, type) else resource
    model = definition.model
    name, column, remote = _get_foreign_key(parent_model, model, fk_name)
    if column.unique or column.primary_key:
        kwargs["max_num"] = 1
    cls = modelformset_factory(
        definition, form=form, formset=formset, extra=extra, can_delete=can_delete, **kwargs
    )
    relations = [
        relation.key
        for relation in sa.inspect(parent_model).relationships
        if relation.mapper.class_ is model
        and any(local is column for _, local in relation.synchronize_pairs)
    ]
    result = cast(type[BaseInlineFormSet], cls)
    result.parent_model, result.fk_name, result.fk, result.to_field = (
        parent_model,
        name,
        column,
        remote,
    )
    result.relation_name = relations[0] if len(relations) == 1 else None
    return result
