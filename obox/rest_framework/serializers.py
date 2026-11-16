from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any, ClassVar, cast

import sqlalchemy as sa

from obox._async import invoke
from obox.db import _session
from obox.db._fields import inspect_fields
from obox.rest_framework.exceptions import ValidationError
from obox.rest_framework.fields import (
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
    ReadOnlyField,
    TimeField,
    UUIDField,
    empty,
)
from obox.rest_framework.settings import api_settings


class Serializer:
    _declared_fields: ClassVar[dict[str, Field]] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        declared: dict[str, Field] = {}
        for parent in reversed(cls.__mro__[1:]):
            declared.update(getattr(parent, "_declared_fields", {}))
        for name, value in list(vars(cls).items()):
            if isinstance(value, Field):
                declared[name] = value
                delattr(cls, name)
        cls._declared_fields = declared

    def __init__(
        self,
        instance: Any = None,
        data: Any = empty,
        *,
        partial: bool = False,
        many: bool = False,
        context: dict[str, Any] | None = None,
    ) -> None:
        self.instance, self.initial_data = instance, data
        self.partial, self.many, self.context = partial, many, context or {}
        self.fields = self.get_fields()
        self._validated = False
        self.validated_data: Any = None
        self._errors: Any = {}

    def get_fields(self) -> dict[str, Field]:
        return deepcopy(self._declared_fields)

    @property
    def errors(self) -> Any:
        if not self._validated:
            raise AssertionError("You must call is_valid() before accessing errors.")
        return self._errors

    @property
    def data(self) -> Any:
        if self.initial_data is not empty and not self._validated:
            raise AssertionError("Call is_valid() before accessing data.")
        if self.instance is not None:
            return (
                [self.to_representation(item) for item in self.instance]
                if self.many
                else self.to_representation(self.instance)
            )
        if self._validated:
            return (
                [self.to_representation(item) for item in self.validated_data]
                if self.many
                else self.to_representation(self.validated_data)
            )
        return {}

    def to_representation(self, instance: Any) -> dict[str, Any]:
        result = {}
        for name, field in self.fields.items():
            if field.write_only:
                continue
            source = field.source or name
            if isinstance(instance, Mapping):
                if source not in instance:
                    continue
                value = instance[source]
            else:
                value = getattr(instance, source)
            result[name] = None if value is None else field.to_representation(value)
        return result

    async def is_valid(self, *, raise_exception: bool = False) -> bool:
        if self.initial_data is empty:
            raise AssertionError("Cannot call is_valid() without data.")
        if not self._validated:
            self._validated = True
            try:
                if self.many:
                    if not isinstance(self.initial_data, list):
                        raise ValidationError(
                            {api_settings.NON_FIELD_ERRORS_KEY: ["Expected a list of items."]}
                        )
                    values, errors = [], []
                    for item in self.initial_data:
                        child = type(self)(data=item, context=self.context, partial=self.partial)
                        valid = await child.is_valid()
                        values.append(child.validated_data)
                        errors.append({} if valid else child.errors)
                    if any(errors):
                        raise ValidationError(errors)
                    self.validated_data = values
                else:
                    self.validated_data = await self.run_validation(self.initial_data)
            except ValidationError as exc:
                self._errors = exc.detail
                self.validated_data = [] if self.many else {}
        if self._errors and raise_exception:
            raise ValidationError(self._errors)
        return not self._errors

    async def run_validation(self, data: Any) -> dict[str, Any]:
        if data is None:
            raise ValidationError({api_settings.NON_FIELD_ERRORS_KEY: ["No data provided"]})
        if not isinstance(data, Mapping):
            raise ValidationError(
                {api_settings.NON_FIELD_ERRORS_KEY: ["Invalid data. Expected a dictionary."]}
            )
        values: dict[str, Any] = {}
        errors = {}
        for name, field in self.fields.items():
            if field.read_only:
                continue
            raw = data.get(name, empty)
            if raw is empty and self.partial:
                continue
            try:
                value = await field.run_validation(raw)
                if value is empty:
                    continue
                method = getattr(self, "validate_" + name, None)
                values[field.source or name] = await invoke(method, value) if method else value
            except ValidationError as exc:
                errors[name] = exc.detail
        if errors:
            raise ValidationError(errors)
        try:
            return cast(dict[str, Any], await invoke(self.validate, values))
        except ValidationError as exc:
            if isinstance(exc.detail, dict):
                raise
            raise ValidationError({api_settings.NON_FIELD_ERRORS_KEY: exc.detail}) from exc

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        return attrs

    async def save(self, **kwargs: Any) -> Any:
        if not self._validated or self._errors:
            raise AssertionError("Call is_valid() successfully before saving.")
        if self.many:
            if self.instance is not None:
                raise NotImplementedError("Multiple updates require an explicit implementation.")
            self.instance = [
                await invoke(self.create, {**item, **kwargs}) for item in self.validated_data
            ]
        else:
            values = {**self.validated_data, **kwargs}
            self.instance = (
                await invoke(self.create, values)
                if self.instance is None
                else await invoke(self.update, self.instance, values)
            )
        return self.instance

    def create(self, validated_data: dict[str, Any]) -> Any:
        raise NotImplementedError("Implement create().")

    def update(self, instance: Any, validated_data: dict[str, Any]) -> Any:
        raise NotImplementedError("Implement update().")


class ModelSerializer(Serializer):
    Meta: ClassVar[Any]

    def get_fields(self) -> dict[str, Field]:
        meta = getattr(self, "Meta", None)
        model, names = getattr(meta, "model", None), getattr(meta, "fields", None)
        exclude = getattr(meta, "exclude", None)
        if (
            model is None
            or (names is None and exclude is None)
            or (names is not None and exclude is not None)
        ):
            raise AssertionError(
                "ModelSerializer requires Meta.model and either fields or exclude."
            )
        if isinstance(names, str) and names != "__all__":
            raise TypeError("Meta.fields must be a sequence or '__all__'.")
        specs = inspect_fields(model)
        selected = list(specs) if names is None or names == "__all__" else list(names)
        declared = super().get_fields()
        result: dict[str, Field] = {}
        for name in selected:
            if name in (exclude or ()):
                continue
            if name in declared:
                result[name] = declared[name]
                continue
            if name not in specs:
                raise AssertionError(f"Unknown field {name!r} for {model.__name__}.")
            spec = specs[name]
            generated_pk = spec.column.primary_key and (
                (
                    isinstance(spec.column.type, sa.Integer)
                    and spec.column.autoincrement is not False
                )
                or spec.column.identity is not None
                or spec.column.server_default is not None
            )
            readonly = (
                (spec.readonly and not spec.column.primary_key)
                or generated_pk
                or name in getattr(meta, "read_only_fields", ())
            )
            options = {
                "read_only": readonly,
                "required": (
                    not spec.column.nullable
                    and spec.column.default is None
                    and spec.column.server_default is None
                )
                and not readonly,
                "allow_null": spec.column.nullable,
                **getattr(meta, "extra_kwargs", {}).get(name, {}),
            }
            field: Field
            column = spec.column
            kind = column.type
            if isinstance(kind, sa.Enum):
                field = ChoiceField(
                    choices=[(item.value, item.value) for item in kind.enum_class]
                    if kind.enum_class
                    else [(item, item) for item in kind.enums],
                    **options,
                )
            elif isinstance(kind, sa.String):
                field = CharField(max_length=kind.length, **options)
            elif isinstance(kind, sa.Boolean):
                field = BooleanField(**options)
            elif isinstance(kind, sa.Integer):
                field = IntegerField(**options)
            elif isinstance(kind, sa.Float):
                field = FloatField(**options)
            elif isinstance(kind, sa.Numeric):
                field = DecimalField(
                    max_digits=kind.precision, decimal_places=kind.scale, **options
                )
            elif isinstance(kind, sa.DateTime):
                field = DateTimeField(**options)
            elif isinstance(kind, sa.Date):
                field = DateField(**options)
            elif isinstance(kind, sa.Time):
                field = TimeField(**options)
            elif isinstance(kind, sa.Uuid):
                field = UUIDField(**options)
            else:
                field = JSONField(**options)
            result[name] = field
        return result

    async def run_validation(self, data: Any) -> dict[str, Any]:
        values = await super().run_validation(data)
        model = self.Meta.model
        specs = inspect_fields(model)
        values = {
            name: specs[name].normalize(value) if name in specs else value
            for name, value in values.items()
        }
        mapper = sa.inspect(model)
        constraints = [
            tuple(column.key for column in item.columns)
            for item in mapper.local_table.constraints
            if isinstance(item, (sa.UniqueConstraint, sa.PrimaryKeyConstraint))
        ]
        errors = {}
        async with _session() as session:
            with session.no_autoflush:
                for names in constraints:
                    combined = {
                        name: values.get(name, getattr(self.instance, name, None)) for name in names
                    }
                    if any(value is None for value in combined.values()):
                        continue
                    query = sa.select(mapper.primary_key[0]).where(
                        *(getattr(model, name) == value for name, value in combined.items())
                    )
                    if self.instance is not None:
                        pk = mapper.get_property_by_column(mapper.primary_key[0]).key
                        query = query.where(mapper.primary_key[0] != getattr(self.instance, pk))
                    if await session.scalar(query.limit(1)) is not None:
                        key = names[0] if len(names) == 1 else api_settings.NON_FIELD_ERRORS_KEY
                        errors[key] = (
                            ["This field must be unique."]
                            if len(names) == 1
                            else [f"The fields {', '.join(names)} must make a unique set."]
                        )
                for name, value in values.items():
                    if name not in mapper.column_attrs or value is None:
                        continue
                    for key in mapper.column_attrs[name].columns[0].foreign_keys:
                        if (
                            await session.scalar(
                                sa.select(key.column).where(key.column == value).limit(1)
                            )
                            is None
                        ):
                            errors[name] = [f'Invalid pk "{value}" - object does not exist.']
        if errors:
            raise ValidationError(errors)
        return values

    async def create(self, validated_data: dict[str, Any]) -> Any:
        obj = self.Meta.model(**validated_data)
        async with _session() as session:
            session.add(obj)
            await session.flush()
        return obj

    async def update(self, instance: Any, validated_data: dict[str, Any]) -> Any:
        for name, value in validated_data.items():
            setattr(instance, name, value)
        async with _session() as session:
            session.add(instance)
            await session.flush()
        return instance


__all__ = [
    "BooleanField",
    "CharField",
    "ChoiceField",
    "DateField",
    "DateTimeField",
    "DecimalField",
    "Field",
    "FloatField",
    "IntegerField",
    "JSONField",
    "ModelSerializer",
    "ReadOnlyField",
    "Serializer",
    "TimeField",
    "UUIDField",
    "ValidationError",
]
