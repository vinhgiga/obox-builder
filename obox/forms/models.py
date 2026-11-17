"""Model forms derive their fields from native SQLAlchemy model metadata."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.orm import MANYTOMANY, MANYTOONE, NO_VALUE, selectinload

from obox.core.exceptions import ImproperlyConfigured, ValidationError
from obox.db import _scope, _session
from obox.db._fields import _model_value
from obox.forms.fields import (
    BooleanField,
    CharField,
    ChoiceField,
    DateField,
    DateTimeField,
    DecimalField,
    Field,
    FloatField,
    IntegerField,
    JSONField,
    TimeField,
    UUIDField,
)
from obox.forms.forms import Form
from obox.forms.formsets import BaseFormSet, formset_factory
from obox.forms.relations import ModelChoiceField, ModelMultipleChoiceField
from obox.forms.widgets import HiddenInput


def _editable_relationship(relation: Any) -> bool:
    return "obox_files" in relation.info or (
        not relation.viewonly
        and (
            (relation.direction is MANYTOMANY and relation.uselist)
            or (relation.direction is MANYTOONE and not relation.uselist)
        )
    )


def _relationship_field(relation: Any, **options: Any) -> Field:
    if "obox_files" in relation.info:
        from obox.contrib.files.forms import AttachmentField

        return AttachmentField(
            relation.parent.class_, relation.key, relation.info["obox_files"], **options
        )
    if not _editable_relationship(relation) or len(relation.synchronize_pairs) != 1:
        raise ImproperlyConfigured(f"Unsupported form relationship: {relation.key!r}.")
    if relation.secondary is not None:
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
            raise ImproperlyConfigured(
                f"Relationship {relation.key!r} requires a form for its association model."
            )
        return ModelMultipleChoiceField(
            sa.select(relation.mapper.class_), required=False, **options
        )
    column = relation.synchronize_pairs[0][1]
    return ModelChoiceField(
        sa.select(relation.mapper.class_),
        required=not column.nullable and column.default is None and column.server_default is None,
        **options,
    )


def _check_relationship_columns(mapper: Any, fields: Mapping[str, Field]) -> None:
    for name in fields.keys() & mapper.relationships.keys():
        relation = mapper.relationships[name]
        if relation.direction is MANYTOONE and any(
            mapper.get_property_by_column(local).key in fields
            for _, local in relation.synchronize_pairs
        ):
            raise ImproperlyConfigured(f"Select {name!r} or its foreign key column, not both.")


def fields_for_model(
    model: type[Any],
    fields: Any = None,
    exclude: Any = None,
    *,
    widgets: Any = None,
    labels: Any = None,
    help_texts: Any = None,
) -> dict[str, Field]:
    mapper = sa.inspect(model)
    relationships = {
        relation.key: relation
        for relation in mapper.relationships
        if _editable_relationship(relation)
    }
    foreign_columns = {
        local
        for relation in relationships.values()
        for _, local in relation.synchronize_pairs
        if relation.secondary is None
    }
    names = (
        [
            name
            for name, prop in mapper.column_attrs.items()
            if prop.columns[0] not in foreign_columns
        ]
        + list(relationships)
        if fields in (None, "__all__")
        else list(fields)
    )
    result: dict[str, Field] = {}
    for name in names:
        if name in (exclude or ()):
            continue
        options: dict[str, Any] = {
            "label": (labels or {}).get(name),
            "help_text": (help_texts or {}).get(name, ""),
            "widget": (widgets or {}).get(name),
        }
        if name in mapper.relationships:
            relation = mapper.relationships[name]
            result[name] = _relationship_field(relation, **options)
            continue
        prop = mapper.column_attrs.get(name)
        if prop is None:
            raise ImproperlyConfigured(f"Unknown field {name!r} for {model.__name__}.")
        column = prop.columns[0]
        generated_pk = column.primary_key and (
            (isinstance(column.type, sa.Integer) and column.autoincrement is not False)
            or column.identity is not None
            or column.server_default is not None
        )
        if generated_pk or column.computed is not None or column is mapper.version_id_col:
            if fields not in (None, "__all__"):
                raise ImproperlyConfigured(
                    f"{name!r} cannot be specified as it is a non-editable field."
                )
            continue
        options.update(
            {
                "required": not column.nullable
                and column.default is None
                and column.server_default is None,
            }
        )
        if column.default is not None and column.default.is_scalar:
            options["initial"] = column.default.arg
        field: Field
        kind = column.type
        if isinstance(kind, sa.Enum):
            field = ChoiceField(
                choices=[(item.value, item.value) for item in kind.enum_class]
                if kind.enum_class
                else [(item, item) for item in kind.enums],
                **options,
            )
        elif isinstance(kind, sa.String):
            field = CharField(
                max_length=kind.length, empty_value=None if column.nullable else "", **options
            )
        elif isinstance(kind, sa.Boolean):
            field = BooleanField(**{**options, "required": False})
        elif isinstance(kind, sa.Integer):
            field = IntegerField(**options)
        elif isinstance(kind, sa.Float):
            field = FloatField(**options)
        elif isinstance(kind, sa.Numeric):
            field = DecimalField(max_digits=kind.precision, decimal_places=kind.scale, **options)
        elif isinstance(kind, sa.DateTime):
            field = DateTimeField(**options)
        elif isinstance(kind, sa.Date):
            field = DateField(**options)
        elif isinstance(kind, sa.Time):
            field = TimeField(**options)
        elif isinstance(kind, sa.Uuid):
            field = UUIDField(**options)
        elif isinstance(kind, sa.JSON):
            field = JSONField(**options)
        else:
            raise ImproperlyConfigured(f"Unsupported model field: {name!r}.")
        result[name] = field
    _check_relationship_columns(mapper, result)
    return result


class ModelForm(Form):
    @classmethod
    def _collect_fields(cls) -> dict[str, Field]:
        fields: dict[str, Field] = {}
        for parent in reversed(cls.__mro__[1:]):
            fields.update(getattr(parent, "declared_fields", {}))
        for name, value in list(vars(cls).items()):
            if isinstance(value, Field):
                fields[name] = value
                delattr(cls, name)
            elif value is None:
                fields.pop(name, None)
        return fields

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        meta = getattr(cls, "Meta", None)
        model = getattr(meta, "model", None)
        if model is None:
            return
        selected, exclude = getattr(meta, "fields", None), getattr(meta, "exclude", None)
        if selected is None and exclude is None:
            raise ImproperlyConfigured("A ModelForm requires Meta.fields or Meta.exclude.")
        if isinstance(selected, str) and selected != "__all__":
            raise TypeError("Meta.fields cannot be a string. Use a list or tuple.")
        declared = cls.declared_fields
        names = selected if selected != "__all__" and selected is not None else None
        automatic = [name for name in names if name not in declared] if names is not None else names
        generated = fields_for_model(
            model,
            automatic,
            exclude,
            widgets=getattr(meta, "widgets", None),
            labels=getattr(meta, "labels", None),
            help_texts=getattr(meta, "help_texts", None),
        )
        if names is not None:
            generated = {
                name: declared[name] if name in declared else generated[name]
                for name in names
                if name in declared or name in generated
            }
        generated.update(declared)
        _check_relationship_columns(sa.inspect(model), generated)
        cls.base_fields = generated

    def __init__(
        self,
        data: Any = None,
        files: Any = None,
        *,
        instance: Any = None,
        initial: Mapping[str, Any] | None = None,
        prefix: str | None = None,
        empty_permitted: bool = False,
    ) -> None:
        meta = getattr(self, "Meta", None)
        model = getattr(meta, "model", None)
        if model is None:
            raise ValueError("ModelForm has no model class specified.")
        self.instance = model() if instance is None else instance
        state = sa.inspect(self.instance)
        values = {
            name: state.attrs[name].loaded_value
            for name in self.base_fields
            if name in state.attrs and state.attrs[name].loaded_value is not NO_VALUE
        }
        if instance is None:
            values = {name: field.initial for name, field in self.base_fields.items()}
        super().__init__(
            data,
            files,
            initial={**values, **(initial or {})},
            prefix=prefix,
            empty_permitted=empty_permitted,
        )
        for field in self.fields.values():
            if hasattr(field, "owner_model"):
                cast(Any, field).instance = self.instance

    async def prepare(self) -> None:
        state = sa.inspect(self.instance)
        missing = [
            name
            for name in self.fields
            if name in state.mapper.relationships and name not in self.initial
        ]
        if state.identity is not None and missing:
            async with _session() as session:
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

    async def _full_clean(self) -> None:
        # Related choices share an identity map without flushing the edited instance.
        if not self.is_bound or not any(
            isinstance(field, ModelChoiceField) for field in self.fields.values()
        ):
            await super()._full_clean()
            return
        async with _session() as session:
            with session.no_autoflush:
                if any(field.disabled for field in self.fields.values()):
                    await self.prepare()
                await super()._full_clean()

    def _column_data(self) -> tuple[dict[str, Any], dict[str, str]]:
        mapper = sa.inspect(type(self.instance))
        values = {
            name: value for name, value in self.cleaned_data.items() if name in mapper.column_attrs
        }
        sources = {name: name for name in values}
        for name, value in self.cleaned_data.items():
            if name in mapper.relationships:
                relation = mapper.relationships[name]
                if relation.direction is MANYTOONE:
                    for remote, local in relation.synchronize_pairs:
                        key = mapper.get_property_by_column(local).key
                        values[key] = (
                            getattr(value, relation.mapper.get_property_by_column(remote).key)
                            if value is not None
                            else None
                        )
                        sources[key] = name
        return values, sources

    async def _post_clean(self) -> None:
        mapper = sa.inspect(type(self.instance))
        values, sources = self._column_data()
        constraints = [
            tuple(column.key for column in constraint.columns)
            for constraint in mapper.local_table.constraints
            if isinstance(constraint, (sa.UniqueConstraint, sa.PrimaryKeyConstraint))
        ]
        foreign = [
            (name, key)
            for name, prop in mapper.column_attrs.items()
            for key in prop.columns[0].foreign_keys
            if name in values and values[name] is not None and sources[name] == name
        ]
        relevant = [
            names
            for names in constraints
            if all(name in values and values[name] is not None for name in names)
        ]
        if not relevant and not foreign:
            return
        async with _session() as session:
            with session.no_autoflush:
                for name, key in foreign:
                    found = await session.scalar(
                        sa.select(key.column).where(key.column == values[name]).limit(1)
                    )
                    if found is None:
                        self.add_error(
                            name,
                            "Select a valid choice. "
                            "That choice is not one of the available choices.",
                        )
                for names in relevant:
                    query = sa.select(mapper.primary_key[0]).where(
                        *(
                            getattr(type(self.instance), name)
                            == _model_value(mapper.column_attrs[name].columns[0], values[name])
                            for name in names
                        )
                    )
                    pk = mapper.get_property_by_column(mapper.primary_key[0]).key
                    if not sa.inspect(self.instance).transient:
                        query = query.where(mapper.primary_key[0] != getattr(self.instance, pk))
                    if await session.scalar(query.limit(1)) is not None:
                        labels = " and ".join(name.replace("_", " ") for name in names)
                        self.add_error(
                            sources[names[0]] if len(names) == 1 else None,
                            f"{type(self.instance).__name__} with this {labels} already exists.",
                        )

    async def _validate_save(self) -> None:
        if not self._validated:
            await self.full_clean()
        if not self.is_bound or self.errors:
            raise ValueError(
                f"The {type(self.instance).__name__} could not be saved "
                "because the data didn't validate."
            )

    def _assign_fields(self, session: Any = None) -> None:
        mapper = sa.inspect(type(self.instance))
        for name, value in self.cleaned_data.items():
            if name not in mapper.attrs:
                continue
            column = mapper.column_attrs[name].columns[0] if name in mapper.column_attrs else None
            if name in mapper.relationships:
                relation = mapper.relationships[name]
                if relation.secondary is not None:
                    continue
                if "obox_files" in relation.info:
                    setattr(self.instance, name, value)
                    continue
                if (
                    value is None
                    and self.add_prefix(name) not in self.data
                    and any(local.default is not None for _, local in relation.synchronize_pairs)
                ):
                    continue
                if session is not None and value is not None:
                    value = session.merge(value, load=False)
                for remote, local in relation.synchronize_pairs:
                    setattr(
                        self.instance,
                        mapper.get_property_by_column(local).key,
                        getattr(value, relation.mapper.get_property_by_column(remote).key)
                        if value is not None
                        else None,
                    )
            if (
                column is not None
                and self.add_prefix(name) not in self.data
                and column.default is not None
            ):
                continue
            if column is not None:
                value = _model_value(column, value)
            setattr(self.instance, name, value)

    async def save(self, commit: bool = True) -> Any:
        await self._validate_save()
        if commit:
            async with _session() as session:
                session.add(self.instance)
                with session.no_autoflush:
                    await session.run_sync(self._assign_fields)
                await session.flush()
                await self.save_m2m()
        else:
            self._assign_fields()
        return self.instance

    async def save_m2m(self) -> None:
        """Persist selected collections after the instance has been flushed."""
        await self._validate_save()
        if sa.inspect(self.instance).identity is None:
            raise ValueError("Save the instance before calling save_m2m().")
        mapper = sa.inspect(type(self.instance))
        collections = {
            name: value
            for name, value in self.cleaned_data.items()
            if name in mapper.relationships and mapper.relationships[name].secondary is not None
        }
        if not collections:
            return
        async with _session() as session:
            session.add(self.instance)
            with session.no_autoflush:
                missing = [
                    name for name in collections if name in sa.inspect(self.instance).unloaded
                ]
                if missing:
                    await session.refresh(self.instance, missing)

                def assign(sync_session: Any) -> None:
                    for name, values in collections.items():
                        setattr(
                            self.instance,
                            name,
                            [sync_session.merge(value, load=False) for value in values],
                        )

                await session.run_sync(assign)
            await session.flush()


def modelform_factory(
    model: type[Any],
    form: type[ModelForm] = ModelForm,
    *,
    fields: Any = None,
    exclude: Any = None,
    widgets: Any = None,
    labels: Any = None,
    help_texts: Any = None,
) -> type[ModelForm]:
    base_meta = getattr(form, "Meta", object)
    options = {"model": model}
    options.update(
        {
            name: value
            for name, value in (
                ("fields", fields),
                ("exclude", exclude),
                ("widgets", widgets),
                ("labels", labels),
                ("help_texts", help_texts),
            )
            if value is not None
        }
    )
    meta = type("Meta", (base_meta,), options)
    return type(model.__name__ + "Form", (form,), {"Meta": meta})


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
    edit_only = False

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
        self.saved_forms: list[ModelForm] = []
        super().__init__(data, files, **kwargs)

    def get_queryset(self) -> Any:
        return (
            self.queryset.order_by(self.pk)
            if not self.queryset._order_by_clauses
            else self.queryset
        )

    async def prepare(self) -> None:
        if self._objects is None:
            async with _session() as session:
                with session.no_autoflush:
                    objects = (await session.scalars(self.get_queryset())).unique().all()
            self._objects = {str(getattr(obj, self.pk_name)): obj for obj in objects}
        await super().prepare()

    def initial_form_count(self) -> int:
        return super().initial_form_count() if self.is_bound else len(self._objects or {})

    def _construct_form(self, index: int | None, **kwargs: Any) -> ModelForm:
        if self._objects is None and index is not None:
            raise ValueError("Await formset.prepare() before accessing model forms.")
        if index is not None and index < self.initial_form_count():
            identity = (
                str(self.data.get(f"{self.add_prefix(index)}-{self.pk_name}", ""))
                if self.is_bound
                else list(self._objects or {})[index]
            )
            kwargs["instance"] = (self._objects or {}).get(identity)
        return cast(ModelForm, super()._construct_form(index, **kwargs))

    def add_fields(self, form: Form, index: int | None) -> None:
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
        for form in cast(list[ModelForm], self.forms):
            if form.errors or self._should_delete_form(form) or not form.cleaned_data:
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
            self.errors = [form.errors for form in self.forms if not self._should_delete_form(form)]
            raise ValidationError("Please correct the duplicate data in the forms.")

    async def save_new(self, form: ModelForm, commit: bool = True) -> Any:
        return await form.save(commit=commit)

    async def save_existing(self, form: ModelForm, instance: Any, commit: bool = True) -> Any:
        return await form.save(commit=commit)

    async def delete_existing(self, obj: Any, commit: bool = True) -> None:
        if commit:
            async with _session() as session:
                await session.delete(obj)
                await session.flush()

    async def save(self, commit: bool = True) -> list[Any]:
        if not await self.is_valid():
            raise ValueError("The formset could not be saved because the data didn't validate.")
        self.changed_objects, self.deleted_objects, self.new_objects, self.saved_forms = (
            [],
            [],
            [],
            [],
        )

        async def persist() -> list[Any]:
            saved = []
            for form in cast(list[ModelForm], self.forms):
                existing = sa.inspect(form.instance).identity is not None
                if self._should_delete_form(form):
                    if existing:
                        self.deleted_objects.append(form.instance)
                        await self.delete_existing(form.instance, commit)
                    continue
                if not form.has_changed() or (self.edit_only and not existing):
                    continue
                if existing:
                    saved.append(await self.save_existing(form, form.instance, commit))
                    self.changed_objects.append((form.instance, form.changed_data))
                else:
                    saved.append(await self.save_new(form, commit))
                    self.new_objects.append(form.instance)
                self.saved_forms.append(form)
            return saved

        if commit:
            async with _session():
                return await persist()
        return await persist()

    async def save_m2m(self) -> None:
        for form in self.saved_forms:
            await form.save_m2m()


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
        save_as_new: bool = False,
        **kwargs: Any,
    ) -> None:
        self.instance = self.parent_model() if instance is None else instance
        self.save_as_new = save_as_new
        if save_as_new and data is not None:
            data = data.copy()
            prefix = kwargs.get("prefix") or self.get_default_prefix()
            pk_name = (
                sa.inspect(self.model)
                .get_property_by_column(sa.inspect(self.model).primary_key[0])
                .key
            )
            for key in list(data):
                if key.startswith(prefix + "-") and key.endswith(
                    ("-" + pk_name, "-" + self.fk_name)
                ):
                    data[key] = ""
        query = sa.select(self.model) if queryset is None else _scope(queryset, self.model)
        value = getattr(self.instance, self.to_field, None)
        query = (
            query.where(self.fk == value)
            if value is not None and not save_as_new
            else query.where(sa.false())
        )
        self.unique_fields = {self.fk_name}
        super().__init__(data, files, queryset=query, **kwargs)

    @classmethod
    def get_default_prefix(cls) -> str:
        return cls.relation_name or cls.model.__name__.lower() + "_set"

    def initial_form_count(self) -> int:
        return 0 if self.save_as_new else super().initial_form_count()

    def add_fields(self, form: Form, index: int | None) -> None:
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

    async def save_new(self, form: ModelForm, commit: bool = True) -> Any:
        value = getattr(self.instance, self.to_field, None)
        if value is None and commit:
            raise ValueError("Save the parent instance before saving its inline forms.")
        setattr(form.instance, self.fk_name, value)
        for name, field in form.fields.items():
            if isinstance(field, InlineForeignKeyField):
                form.cleaned_data[name] = self.instance if field.relationship else value
        result = await super().save_new(form, commit)
        if not commit:
            relation = next(
                (
                    relation.key
                    for relation in sa.inspect(self.model).relationships
                    if not relation.uselist
                    and any(local is self.fk for _, local in relation.synchronize_pairs)
                ),
                None,
            )
            if relation is not None:
                setattr(result, relation, self.instance)
        return result


def modelformset_factory(
    model: type[Any],
    form: type[ModelForm] = ModelForm,
    *,
    formset: type[BaseModelFormSet] = BaseModelFormSet,
    fields: Any = None,
    exclude: Any = None,
    widgets: Any = None,
    labels: Any = None,
    help_texts: Any = None,
    edit_only: bool = False,
    **kwargs: Any,
) -> type[BaseModelFormSet]:
    cls = modelform_factory(
        model,
        form,
        fields=fields,
        exclude=exclude,
        widgets=widgets,
        labels=labels,
        help_texts=help_texts,
    )
    result = cast(type[BaseModelFormSet], formset_factory(cls, formset, **kwargs))
    result.model, result.edit_only = model, edit_only
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
    model: type[Any],
    form: type[ModelForm] = ModelForm,
    *,
    formset: type[BaseInlineFormSet] = BaseInlineFormSet,
    fk_name: str | None = None,
    extra: int = 3,
    can_delete: bool = True,
    **kwargs: Any,
) -> type[BaseInlineFormSet]:
    name, column, remote = _get_foreign_key(parent_model, model, fk_name)
    if column.unique or column.primary_key:
        kwargs["max_num"] = 1
    cls = modelformset_factory(
        model, form, formset=formset, extra=extra, can_delete=can_delete, **kwargs
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
