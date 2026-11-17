"""Annotation-driven field declarations and validated value descriptors."""

from __future__ import annotations

import types
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from functools import reduce
from inspect import get_annotations
from operator import or_
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    ClassVar,
    Literal,
    TypedDict,
    TypeVar,
    Union,
    Unpack,
    get_args,
    get_origin,
    get_type_hints,
    overload,
)
from uuid import UUID

from pydantic import AfterValidator, BeforeValidator, PlainValidator, TypeAdapter, WrapValidator
from pydantic import Field as PydanticField
from pydantic import ValidationError as PydanticValidationError

from obox._async import invoke
from obox.core.exceptions import ImproperlyConfigured, ValidationError
from obox.forms.fields import (
    BooleanField,
    CharField,
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
from obox.forms.widgets import Select, Widget

if TYPE_CHECKING:
    from obox.forms.forms import Form


class _Missing(Enum):
    VALUE = "missing"


MISSING = _Missing.VALUE
_T = TypeVar("_T")


class _Options(TypedDict, total=False):
    label: str | None
    help_text: str
    initial: Any
    disabled: bool
    validators: Sequence[Callable[..., Any]]
    error_messages: Mapping[str, str]
    ge: int | float | Decimal
    le: int | float | Decimal
    min_length: int
    max_length: int


class _Declaration:
    def __init__(
        self,
        widget: type[Widget] | Widget | None,
        default: Any,
        default_factory: Callable[[], Any] | None,
        options: _Options,
    ) -> None:
        if default is not MISSING and default_factory is not None:
            raise TypeError("Specify default or default_factory, not both.")
        if widget is not None and not (
            isinstance(widget, Widget) or (isinstance(widget, type) and issubclass(widget, Widget))
        ):
            raise ImproperlyConfigured("A form widget must be a Widget class or instance.")
        self.widget, self.default, self.default_factory = widget, default, default_factory
        self.options = options


@overload
def field(
    widget: type[Widget] | Widget | None = None,
    *,
    default: _T,
    **options: Unpack[_Options],
) -> _T: ...


@overload
def field(
    widget: type[Widget] | Widget | None = None,
    *,
    default_factory: Callable[[], _T],
    **options: Unpack[_Options],
) -> _T: ...


@overload
def field(
    widget: type[Widget] | Widget | None = None,
    **options: Unpack[_Options],
) -> Any: ...


def field(
    widget: type[Widget] | Widget | None = None,
    *,
    default: Any = MISSING,
    default_factory: Callable[[], Any] | None = None,
    **options: Unpack[_Options],
) -> Any:
    return _Declaration(widget, default, default_factory, options)


def _kind(annotation: Any) -> tuple[Any, bool]:
    if get_origin(annotation) is Annotated:
        return _kind(get_args(annotation)[0])
    if get_origin(annotation) in (Union, types.UnionType):
        args = get_args(annotation)
        non_null = [arg for arg in args if arg is not type(None)]
        if len(non_null) == 1 and len(args) == 2:
            return _kind(non_null[0])[0], True
    return annotation, False


def _output_type(annotation: Any) -> Any:
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is Annotated:
        metadata = [
            item
            for item in args[1:]
            if not isinstance(
                item, (AfterValidator, BeforeValidator, PlainValidator, WrapValidator)
            )
        ]
        base = _output_type(args[0])
        return Annotated[(base, *metadata)] if metadata else base
    if origin in (Union, types.UnionType):
        return reduce(or_, (_output_type(arg) for arg in args))
    if origin in (list, dict):
        return types.GenericAlias(origin, tuple(_output_type(arg) for arg in args))
    return annotation


class TypedField(Field):
    def __init__(self, annotation: Any, declaration: _Declaration) -> None:
        kind, self.nullable = _kind(annotation)
        self.default, self.default_factory = declaration.default, declaration.default_factory
        self.has_default = self.default is not MISSING or self.default_factory is not None
        options: dict[str, Any] = dict(declaration.options)
        constraints: dict[str, Any] = {
            name: options.pop(name)
            for name in ("ge", "le", "min_length", "max_length")
            if name in options
        }
        super().__init__(
            required=not self.has_default and not self.nullable,
            widget=declaration.widget,
            initial=options.pop("initial", MISSING),
            **options,
        )
        choices = None
        if isinstance(kind, type) and issubclass(kind, Enum):
            choices = [(member.value, member.value) for member in kind]
            kind = type(choices[0][0]) if choices else str
        elif get_origin(kind) is Literal:
            choices = [(value, value) for value in get_args(kind)]
            kinds = {type(value) for value, _ in choices}
            kind = kinds.pop() if len(kinds) == 1 else None
        converters: dict[Any, type[Field]] = {
            str: CharField,
            int: IntegerField,
            float: FloatField,
            Decimal: DecimalField,
            bool: BooleanField,
            date: DateField,
            datetime: DateTimeField,
            time: TimeField,
            UUID: UUIDField,
            dict: JSONField,
            list: JSONField,
        }
        converter = converters.get(get_origin(kind) or kind)
        if converter is None:
            raise ImproperlyConfigured(f"Unsupported form annotation: {annotation!r}.")
        converter_options: dict[str, Any] = {}
        if converter is CharField:
            converter_options = {
                name: value for name, value in constraints.items() if name.endswith("length")
            }
            converter_options["empty_value"] = None if self.nullable else ""
        elif converter is IntegerField:
            converter_options = {
                f"{name}_value": constraints[key]
                for name, key in (("min", "ge"), ("max", "le"))
                if key in constraints
            }
        self.converter = converter(required=False, **converter_options)
        self.requires_true = choices is None and converter is BooleanField
        if declaration.widget is None:
            self.widget = Select(choices=choices) if choices is not None else self.converter.widget
        elif choices is not None and isinstance(self.widget, Select):
            self.widget.choices = choices
        for key, attr in (
            ("ge", "min"),
            ("le", "max"),
            ("min_length", "minlength"),
            ("max_length", "maxlength"),
        ):
            if key in constraints:
                self.widget.attrs.setdefault(attr, constraints[key])
        self.is_json = isinstance(self.converter, JSONField)
        input_type: Any = Annotated[annotation, PydanticField(**constraints)]
        output_type: Any = Annotated[_output_type(annotation), PydanticField(**constraints)]
        self.adapter: TypeAdapter[Any] = TypeAdapter(input_type)
        self.output_adapter: TypeAdapter[Any] = TypeAdapter(output_type)

    def __deepcopy__(self, memo: dict[int, Any]) -> TypedField:
        result = type(self).__new__(type(self))
        memo[id(self)] = result
        result.__dict__ = {
            key: value if key in ("adapter", "output_adapter") else deepcopy(value, memo)
            for key, value in vars(self).items()
        }
        return result

    def get_default(self) -> Any:
        return (
            self.default_factory() if self.default_factory is not None else deepcopy(self.default)
        )

    def validate(self, value: Any, *, strict: bool = False) -> Any:
        if value is MISSING or (
            self.required and (value in self.empty_values or (self.requires_true and not value))
        ):
            raise ValidationError(self.error_messages["required"], code="required")
        try:
            adapter = self.output_adapter if strict else self.adapter
            return adapter.validate_python(value, strict=strict)
        except PydanticValidationError as exc:
            raise ValidationError(self.error_messages["invalid"], code="invalid") from exc

    async def clean(self, value: Any) -> Any:
        if value is MISSING:
            return self.validate(value)
        if isinstance(value, Enum):
            value = value.value
        if isinstance(value, str):
            value = value.strip()
        if value in (None, "") and self.nullable:
            value = None
        else:
            if self.required and value in self.empty_values:
                return self.validate(value)
            if not (self.is_json and isinstance(value, (dict, list))):
                value = self.converter.to_python(value)
        value = self.validate(value)
        if value not in self.empty_values or isinstance(self.converter, BooleanField):
            for validator in self.validators:
                await invoke(validator, value)
        return value


class _Value:
    def __init__(self, name: str, field: TypedField) -> None:
        self.name, self.field = name, field

    def __get__(self, instance: Form | None, owner: type[Form] | None = None) -> Any:
        if instance is None:
            return self
        if (
            not instance._validated
            or instance.errors
            or instance._values is None
            or self.name not in instance._values
        ):
            raise RuntimeError("Field values are available only after successful form validation.")
        return instance._values[self.name]

    def __set__(self, instance: Form, value: Any) -> None:
        raise AttributeError("Validated form values are read-only.")


def collect_fields(cls: type[Form], base: type[Form], reserved: set[str]) -> dict[str, Field]:
    fields: dict[str, Field] = {}
    for parent in reversed(cls.__mro__):
        if parent is base or parent is object:
            continue
        try:
            hints = get_type_hints(parent, include_extras=True)
            annotations = get_annotations(parent)
        except (NameError, TypeError) as exc:
            raise ImproperlyConfigured(f"Cannot resolve annotations for {cls.__name__}.") from exc
        for name in annotations:
            annotation = hints[name]
            if name.startswith("_") or get_origin(annotation) is ClassVar:
                fields.pop(name, None)
                continue
            if name in reserved:
                raise ImproperlyConfigured(f"Form field {name!r} conflicts with the form API.")
            value = vars(parent).get(name, MISSING)
            if isinstance(value, _Value):
                fields[name] = value.field
                continue
            if isinstance(value, Field):
                raise ImproperlyConfigured("Use an annotation and field(), not legacy form fields.")
            declaration = (
                value if isinstance(value, _Declaration) else _Declaration(None, value, None, {})
            )
            try:
                fields[name] = TypedField(annotation, declaration)
            except (TypeError, ValueError) as exc:
                raise ImproperlyConfigured(f"Invalid declaration for form field {name!r}.") from exc
        for name, value in vars(parent).items():
            if name not in annotations and (
                isinstance(value, (Field, _Declaration))
                or (name in fields and not isinstance(value, _Value))
            ):
                raise ImproperlyConfigured(f"Form field {name!r} requires a type annotation.")
    for name, bound in fields.items():
        if isinstance(bound, TypedField):
            setattr(cls, name, _Value(name, bound))
    return fields
