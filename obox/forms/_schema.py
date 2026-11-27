"""Pydantic owns value constraints; field objects describe HTML controls."""

from __future__ import annotations

import types
from decimal import Decimal
from enum import Enum
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel
from starlette.datastructures import UploadFile

from obox.forms.fields import (
    BooleanField,
    ChoiceField,
    Field,
    FileField,
    JSONField,
)
from obox.forms.widgets import NumberInput, SelectMultiple

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
    widgets = {
        int: NumberInput,
        float: NumberInput,
        Decimal: NumberInput,
    }
    for name, info in schema.model_fields.items():
        annotation, nullable = kind(info.annotation)
        options: dict[str, Any] = {
            "required": info.is_required() and not nullable,
            "label": info.title,
            "help_text": info.description or "",
        }
        # An unchecked HTML checkbox is omitted from a form submission and
        # means False, even when the underlying Pydantic boolean is required.
        # Pydantic still validates the resulting value (including Literal[True]).
        if annotation is bool or (
            get_origin(annotation) is Literal
            and all(isinstance(value, bool) for value in get_args(annotation))
        ):
            options["required"] = False
        field: Field
        if annotation is bool or (
            get_origin(annotation) is Literal
            and all(isinstance(value, bool) for value in get_args(annotation))
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
            if isinstance(annotation, type) and issubclass(annotation, UploadFile):
                field = FileField(**options)
            elif get_origin(annotation) in {dict, list} or annotation in {dict, list}:
                field = JSONField(**options)
                if get_origin(annotation) is list:
                    field.widget = SelectMultiple()
            else:
                field = Field(widget=widgets.get(annotation), **options)
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
