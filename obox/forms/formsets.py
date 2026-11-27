"""Bounded editing of children through their parent form."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping
from functools import cached_property
from typing import Any, ClassVar

import sqlalchemy as sa
from pydantic import BaseModel
from pydantic import Field as SchemaField
from pydantic import ValidationError as SchemaValidationError

from obox.core.async_utils import invoke
from obox.core.exceptions import ValidationError
from obox.core.principal import ANONYMOUS
from obox.db.query import scope_query
from obox.db.session import session_scope
from obox.forms.fields import BooleanField, Field
from obox.forms.forms import Form
from obox.forms.models import ModelForm, modelform_factory
from obox.forms.widgets import CheckboxInput, HiddenInput
from obox.resources import ModelResource

DEFAULT_MAX_NUM = 1000


class ManagementInput(BaseModel):
    TOTAL_FORMS: int = SchemaField(ge=0)
    INITIAL_FORMS: int = SchemaField(ge=0)


class ManagementForm(Form[ManagementInput]):
    schema = ManagementInput
    widgets = dict.fromkeys(ManagementInput.model_fields, HiddenInput)


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


class BaseInlineFormSet:
    form: type[ModelForm[Any]]
    resource: ClassVar[ModelResource[Any]]
    model: type[Any]
    parent_model: type[Any]
    fk: Any
    fk_name: str
    to_field: str
    relation_name: str | None = None
    extra = 3
    min_num = 0
    max_num = DEFAULT_MAX_NUM
    absolute_max = DEFAULT_MAX_NUM * 2
    validate_min = False
    validate_max = False
    can_delete = True
    can_delete_extra = True
    deletion_widget = CheckboxInput
    default_error_messages: ClassVar[dict[str, str]] = {
        "missing_management_form": "ManagementForm data is missing or has been tampered with.",
        "too_many_forms": "Please submit at most %(num)d forms.",
        "too_few_forms": "Please submit at least %(num)d forms.",
    }

    def __init__(
        self,
        data: Any = None,
        files: Any = None,
        instance: Any = None,
        *,
        prefix: str | None = None,
        queryset: Any = None,
        form_kwargs: Mapping[str, Any] | None = None,
        error_messages: Mapping[str, str] | None = None,
    ) -> None:
        self.instance = self.parent_model() if instance is None else instance
        query = sa.select(self.model) if queryset is None else scope_query(queryset, self.model)
        value = getattr(self.instance, self.to_field, None)
        self.queryset = query.where(self.fk == value if value is not None else sa.false())
        mapper = sa.inspect(self.model)
        self.pk = mapper.primary_key[0]
        self.pk_name = mapper.get_property_by_column(self.pk).key
        self._objects: dict[str, Any] | None = None
        self.is_bound = data is not None or files is not None
        self.data, self.files = {} if data is None else data, files or {}
        self.prefix = prefix if prefix is not None else self.get_default_prefix()
        self.form_kwargs = dict(form_kwargs or {})
        self.error_messages = {**self.default_error_messages, **(error_messages or {})}
        self.errors: list[dict[str, list[str]]] = []
        self._non_form_errors: list[str] = []
        self._validated = False
        self._validation_lock = asyncio.Lock()
        self.changed_objects: list[tuple[Any, list[str]]] = []
        self.deleted_objects: list[Any] = []
        self.new_objects: list[Any] = []

    @classmethod
    def get_default_prefix(cls) -> str:
        return cls.relation_name or cls.model.__name__.lower() + "_set"

    def add_prefix(self, index: int | str) -> str:
        return f"{self.prefix}-{index}"

    @cached_property
    def management_form(self) -> ManagementForm:
        return ManagementForm(
            self.data if self.is_bound else None,
            prefix=self.prefix,
            initial={
                "TOTAL_FORMS": self.total_form_count(),
                "INITIAL_FORMS": self.initial_form_count(),
            },
        )

    @cached_property
    def _submitted_counts(self) -> ManagementInput | None:
        keys = {name: self.add_prefix(name) for name in ManagementInput.model_fields}
        getlist = getattr(self.data, "getlist", None)
        if getlist is not None and any(len(getlist(key)) != 1 for key in keys.values()):
            return None
        try:
            counts = ManagementInput.model_validate(
                {name: self.data.get(key) for name, key in keys.items()}
            )
        except SchemaValidationError:
            return None
        return counts if counts.INITIAL_FORMS <= counts.TOTAL_FORMS else None

    def initial_form_count(self) -> int:
        if not self.is_bound:
            return len(self._objects or {})
        return self._submitted_counts.INITIAL_FORMS if self._submitted_counts else 0

    def total_form_count(self) -> int:
        if self.is_bound:
            counts = self._submitted_counts
            return counts.TOTAL_FORMS if counts and self.absolute_max >= counts.TOTAL_FORMS else 0
        initial = self.initial_form_count()
        return max(initial, min(max(initial, self.min_num) + self.extra, self.max_num))

    def get_form_kwargs(self, index: int | None) -> dict[str, Any]:
        return self.form_kwargs.copy()

    def get_queryset(self) -> Any:
        return self.queryset if self.queryset._order_by_clauses else self.queryset.order_by(self.pk)

    async def prepare(self) -> None:
        if self.is_bound and not self.total_form_count():
            self._objects = {}
            return
        if self._objects is None:
            async with session_scope() as session:
                ctx = session.info.get("obox.runtime.context")
                bound = self.resource.bind(session, ctx.principal if ctx else ANONYMOUS)
                with session.no_autoflush:
                    objects = (
                        (await session.scalars(bound._query(self.get_queryset()))).unique().all()
                    )
            self._objects = {str(getattr(obj, self.pk_name)): obj for obj in objects}
        for form in self.forms:
            await form.prepare()

    def construct_form(self, index: int | None, **kwargs: Any) -> ModelForm[Any]:
        options = self.get_form_kwargs(index)
        initial_count = self.initial_form_count()
        if index is not None:
            if self._objects is None:
                raise ValueError("Await formset.prepare() before accessing model forms.")
            if self.is_bound:
                options.update(data=self.data, files=self.files)
            if index < initial_count:
                identity = (
                    str(self.data.get(f"{self.add_prefix(index)}-{self.pk_name}", ""))
                    if self.is_bound
                    else list(self._objects)[index]
                )
                options["instance"] = self._objects.get(identity)
        form = self.form(
            **{
                **options,
                "prefix": self.add_prefix("__prefix__" if index is None else index),
                "empty_permitted": index is None or index >= max(initial_count, self.min_num),
                **kwargs,
            }
        )
        form.use_required_attribute = False
        self.add_fields(form, index)
        return form

    def add_fields(self, form: ModelForm[Any], index: int | None) -> None:
        existing = index is not None and index < self.initial_form_count()
        if self.can_delete and (self.can_delete_extra or existing):
            form.fields["DELETE"] = BooleanField(
                label="Delete", required=False, widget=self.deletion_widget
            )
        if self.pk_name not in form.fields:
            form.fields[self.pk_name] = _IdentityField(
                self._objects or {}, self.pk_name, required=existing
            )
        form.initial[self.pk_name] = getattr(form.instance, self.pk_name, None)
        relationships = sa.inspect(self.model).relationships
        name = next(
            (
                name
                for name in form.fields
                if name in relationships
                and any(local is self.fk for _, local in relationships[name].synchronize_pairs)
            ),
            self.fk_name,
        )
        field = InlineForeignKeyField(
            self.instance, to_field=self.to_field, relationship=name in relationships
        )
        form.fields[name] = field
        form.initial[name] = field.initial

    @cached_property
    def forms(self) -> list[ModelForm[Any]]:
        return [self.construct_form(index) for index in range(self.total_form_count())]

    @property
    def initial_forms(self) -> list[ModelForm[Any]]:
        return self.forms[: self.initial_form_count()]

    @property
    def extra_forms(self) -> list[ModelForm[Any]]:
        return self.forms[self.initial_form_count() :]

    @property
    def empty_form(self) -> ModelForm[Any]:
        return self.construct_form(None)

    def __iter__(self) -> Iterator[ModelForm[Any]]:
        return iter(self.forms)

    def __getitem__(self, index: int) -> ModelForm[Any]:
        return self.forms[index]

    def __len__(self) -> int:
        return len(self.forms)

    def __bool__(self) -> bool:
        return True

    def is_multipart(self) -> bool:
        return (self.forms[0] if self.forms else self.empty_form).is_multipart()

    def should_delete_form(self, form: ModelForm[Any]) -> bool:
        return self.can_delete and bool(form.validated_data.get("DELETE"))

    @property
    def deleted_forms(self) -> list[ModelForm[Any]]:
        return (
            [form for form in self.forms if self.should_delete_form(form)] if self._valid() else []
        )

    @property
    def cleaned_data(self) -> list[dict[str, Any]]:
        if not self._valid():
            raise AttributeError("cleaned_data is available after successful validation.")
        return [form.validated_data for form in self.forms]

    def _valid(self) -> bool:
        return self._validated and not self._non_form_errors and not any(self.errors)

    async def is_valid(self) -> bool:
        if not self.is_bound:
            return False
        async with self._validation_lock:
            if not self._validated:
                await self.full_clean()
        return self._valid()

    async def full_clean(self) -> None:
        self._validated = False
        self.errors, self._non_form_errors = [], []
        if not self.is_bound:
            return
        counts = self._submitted_counts
        if counts is None:
            self._non_form_errors.append(self.error_messages["missing_management_form"])
        elif self.absolute_max < counts.TOTAL_FORMS:
            self._non_form_errors.append(
                self.error_messages["too_many_forms"] % {"num": self.absolute_max}
            )
        else:
            await self.prepare()
            await self._clean_forms(counts.INITIAL_FORMS)
        self._validated = True

    async def _clean_forms(self, initial_count: int) -> None:
        empty = deleted = 0
        identities = set()
        for index, form in enumerate(self.forms):
            await form.full_clean()
            if index < initial_count:
                identity = sa.inspect(form.instance).identity
                if identity is None or identity in identities:
                    self._non_form_errors.append("Select a unique existing inline record.")
                    form.add_error(None, self._non_form_errors[-1])
                    self.errors.append(form.errors)
                    continue
                identities.add(identity)
            if self.should_delete_form(form):
                deleted += 1
            else:
                self.errors.append(form.errors)
                empty += index >= initial_count and not form.has_changed()
        try:
            remaining = len(self.forms) - deleted
            if self.validate_max and remaining > self.max_num:
                raise ValidationError(
                    self.error_messages["too_many_forms"], params={"num": self.max_num}
                )
            if self.validate_min and remaining - empty < self.min_num:
                raise ValidationError(
                    self.error_messages["too_few_forms"], params={"num": self.min_num}
                )
            await invoke(self.clean)
        except ValidationError as exc:
            self._non_form_errors.extend(exc.messages)

    def clean(self) -> None:
        self.validate_unique()

    def validate_unique(self) -> None:
        seen: dict[tuple[str, ...], set[tuple[Any, ...]]] = {
            tuple(column.key for column in constraint.columns): set()
            for constraint in sa.inspect(self.model).local_table.constraints
            if isinstance(constraint, (sa.UniqueConstraint, sa.PrimaryKeyConstraint))
        }
        duplicates = False
        for form in self.forms:
            if form.errors or self.should_delete_form(form) or not form.validated_data:
                continue
            values, _ = form._column_data()
            for names, rows in seen.items():
                if not all(name in values for name in names):
                    continue
                row = tuple(
                    "__parent__" if name == self.fk_name else values[name] for name in names
                )
                if None in row:
                    continue
                if row in rows:
                    form.add_error(None, "Please correct the duplicate data below.")
                    duplicates = True
                rows.add(row)
        if duplicates:
            raise ValidationError("Please correct the duplicate data in the forms.")

    def non_form_errors(self) -> list[str]:
        return self._non_form_errors

    def total_error_count(self) -> int:
        return len(self._non_form_errors) + sum(len(errors) for errors in self.errors)

    def has_changed(self) -> bool:
        return any(form.has_changed() for form in self.forms)

    async def save_new(self, form: ModelForm[Any]) -> Any:
        value = getattr(self.instance, self.to_field, None)
        if value is None:
            raise ValueError("Save the parent instance before saving its inline forms.")
        setattr(form.instance, self.fk_name, value)
        for name, field in form.fields.items():
            if isinstance(field, InlineForeignKeyField):
                form.validated_data[name] = self.instance if field.relationship else value
        return await form.save()

    async def save_existing(self, form: ModelForm[Any], instance: Any) -> Any:
        return await form.save()

    async def delete_existing(self, obj: Any) -> None:
        async with session_scope() as session:
            ctx = session.info.get("obox.runtime.context")
            await self.resource.bind(session, ctx.principal if ctx else ANONYMOUS).destroy(obj)

    async def save(self) -> list[Any]:
        if not await self.is_valid():
            raise ValueError("The formset could not be saved because the data didn't validate.")
        self.changed_objects, self.deleted_objects, self.new_objects = [], [], []
        saved = []
        async with session_scope():
            for form in self.forms:
                existing = sa.inspect(form.instance).identity is not None
                if self.should_delete_form(form):
                    if existing:
                        await self.delete_existing(form.instance)
                        self.deleted_objects.append(form.instance)
                elif form.has_changed():
                    if existing:
                        saved.append(await self.save_existing(form, form.instance))
                        self.changed_objects.append((form.instance, form.changed_data))
                    else:
                        saved.append(await self.save_new(form))
                        self.new_objects.append(form.instance)
        return saved


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
    if len(candidates) != 1 or len(candidates[0][1].foreign_keys) != 1:
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
    max_num: int | None = None,
    validate_max: bool = False,
    min_num: int | None = None,
    validate_min: bool = False,
    absolute_max: int | None = None,
    can_delete_extra: bool = True,
) -> type[BaseInlineFormSet]:
    definition = resource() if isinstance(resource, type) else resource
    model_form = modelform_factory(definition, form=form)
    model = definition.model
    name, column, remote = _get_foreign_key(parent_model, model, fk_name)
    maximum = 1 if column.unique or column.primary_key else max_num
    maximum = DEFAULT_MAX_NUM if maximum is None else maximum
    absolute = maximum + DEFAULT_MAX_NUM if absolute_max is None else absolute_max
    minimum = 0 if min_num is None else min_num
    if (
        any(type(value) is not int or value < 0 for value in (extra, minimum, maximum, absolute))
        or absolute < maximum
    ):
        raise ValueError("Form counts must be non-negative integers and absolute_max >= max_num.")
    relations = [
        relation.key
        for relation in sa.inspect(parent_model).relationships
        if relation.mapper.class_ is model
        and any(local is column for _, local in relation.synchronize_pairs)
    ]
    return type(
        model_form.__name__ + "Set",
        (formset,),
        {
            "form": model_form,
            "resource": definition,
            "model": model,
            "parent_model": parent_model,
            "fk": column,
            "fk_name": name,
            "to_field": remote,
            "relation_name": relations[0] if len(relations) == 1 else None,
            "extra": extra,
            "min_num": minimum,
            "max_num": maximum,
            "absolute_max": absolute,
            "can_delete": can_delete,
            "can_delete_extra": can_delete_extra,
            "validate_min": validate_min,
            "validate_max": validate_max,
        },
    )
