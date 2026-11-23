"""Pydantic owns value constraints; field objects describe HTML controls."""

from __future__ import annotations

import types
from copy import deepcopy
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from typing import Annotated, Any, Literal, Union, get_args, get_origin
from uuid import UUID

from pydantic import BaseModel, ConfigDict, create_model
from pydantic import Field as SchemaField
from starlette.datastructures import UploadFile

from obox.forms.fields import (
    BooleanField,
    CharField,
    ChoiceField,
    DateField,
    DateTimeField,
    DecimalField,
    Field,
    FileField,
    FloatField,
    IntegerField,
    JSONField,
    TimeField,
    UUIDField,
)
from obox.forms.widgets import SelectMultiple

MISSING = object()


def kind(annotation: Any) -> tuple[Any, bool]:
    if get_origin(annotation) is Annotated:
        return kind(get_args(annotation)[0])
    if get_origin(annotation) in (Union, types.UnionType):
        args = get_args(annotation)
        non_null = [item for item in args if item is not type(None)]
        if len(non_null) == 1 and len(args) == 2:
            return kind(non_null[0])[0], True
    return annotation, False


def schema_fields(schema: type[BaseModel]) -> dict[str, Field]:
    result: dict[str, Field] = {}
    converters = {
        str: CharField,
        int: IntegerField,
        float: FloatField,
        Decimal: DecimalField,
        bool: BooleanField,
        date: DateField,
        datetime: DateTimeField,
        time: TimeField,
        UUID: UUIDField,
        UploadFile: FileField,
        dict: JSONField,
        list: JSONField,
    }
    for name, info in schema.model_fields.items():
        annotation, nullable = kind(info.annotation)
        options: dict[str, Any] = {
            "required": info.is_required() and not nullable,
            "label": info.title,
            "help_text": info.description or "",
        }
        field: Field
        if get_origin(annotation) is Literal and all(
            isinstance(value, bool) for value in get_args(annotation)
        ):
            field = BooleanField(**options)
        elif get_origin(annotation) is Literal:
            field = ChoiceField(
                choices=[(value, value) for value in get_args(annotation)], **options
            )
        elif isinstance(annotation, type) and issubclass(annotation, Enum):
            field = ChoiceField(
                choices=[(value.value, value.value) for value in annotation], **options
            )
        else:
            converter = converters.get(get_origin(annotation) or annotation, Field)
            if isinstance(annotation, type) and issubclass(annotation, UploadFile):
                converter = FileField
            field = converter(**options)
            if isinstance(field, CharField):
                field.strip = False
            if get_origin(annotation) is list:
                field.widget = SelectMultiple()
        control: Any = field.widget
        for metadata in info.metadata:
            for key, attr in (
                ("ge", "min"),
                ("le", "max"),
                ("min_length", "minlength"),
                ("max_length", "maxlength"),
            ):
                value = getattr(metadata, key, None)
                if value is not None:
                    control.attrs[attr] = value
        result[name] = field
    return result


def fields_schema(name: str, fields: dict[str, Field]) -> type[BaseModel]:
    """Derive model form input types from mapped control metadata."""
    definitions: dict[str, Any] = {}
    for key, field in fields.items():
        annotation: Any = Any
        constraints: dict[str, Any] = {}
        if isinstance(field, CharField):
            annotation = str
            if field.required:
                constraints["min_length"] = 1
            for attr in ("min_length", "max_length"):
                if (value := getattr(field, attr)) is not None:
                    constraints[attr] = value
        elif isinstance(field, BooleanField):
            annotation = bool
        elif isinstance(field, IntegerField):
            annotation = int
            for attr, target in (("min_value", "ge"), ("max_value", "le")):
                if (value := getattr(field, attr)) is not None:
                    constraints[target] = value
        elif isinstance(field, DecimalField):
            annotation = Decimal
            constraints = {
                key: value
                for key in ("max_digits", "decimal_places")
                if (value := getattr(field, key)) is not None
            }
        elif isinstance(field, FloatField):
            annotation = float
            constraints["allow_inf_nan"] = False
        elif isinstance(field, (DateField, DateTimeField, TimeField, UUIDField)):
            annotation = field.python_type
        elif isinstance(field, FileField):
            annotation = UploadFile
        if not field.required and annotation not in (Any, bool, str):
            annotation = annotation | None
        default: Any = ... if field.required else deepcopy(field.initial)
        if isinstance(field, BooleanField) and not field.required:
            default = bool(field.initial)
        elif isinstance(field, CharField) and not field.required and default is None:
            default = field.empty_value
        definitions[key] = (annotation, SchemaField(default=default, **constraints))
    return create_model(name, __config__=ConfigDict(arbitrary_types_allowed=True), **definitions)
