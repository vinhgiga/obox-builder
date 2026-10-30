"""Compile scalar SQLAlchemy metadata into explicit Pydantic projections."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from typing import Annotated, Any
from uuid import UUID

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model
from sqlalchemy.exc import NoInspectionAvailable
from sqlalchemy.orm import Mapper
from sqlalchemy.sql.functions import FunctionElement

from obox_builder.errors import BuilderValidationError, ImproperlyConfigured

FieldRefs = Iterable[Any]


@dataclass(frozen=True)
class ModelField:
    name: str
    column: Any
    annotation: Any
    required: bool
    readonly: bool
    exposed: bool


def inspect_fields(model: type[Any]) -> dict[str, ModelField]:
    try:
        mapper = sa.inspect(model)
    except NoInspectionAvailable as exc:
        raise ImproperlyConfigured("Register a SQLAlchemy mapped class.") from exc
    if not isinstance(mapper, Mapper) or len(mapper.primary_key) != 1:
        raise ImproperlyConfigured("Resources require a mapped class with one primary key.")
    result = {}
    for attr in mapper.column_attrs:
        column = attr.columns[0]
        if len(attr.columns) != 1 or not isinstance(column, sa.Column):
            continue
        try:
            annotation: Any = column.type.python_type
        except NotImplementedError:
            continue
        if (
            annotation not in {str, int, bool, float, Decimal, date, datetime, time, UUID}
            and not (isinstance(annotation, type) and issubclass(annotation, Enum))
            and not isinstance(column.type, sa.JSON)
        ):
            continue
        if isinstance(column.type, sa.JSON):
            annotation = Any
        if annotation is str and isinstance(column.type, sa.String) and column.type.length:
            annotation = Annotated[annotation, Field(max_length=column.type.length)]
        if column.nullable:
            annotation = annotation | None
        readonly = bool(
            column.primary_key
            or column is mapper.version_id_col
            or column.computed is not None
            or column.identity is not None
            or column.server_onupdate is not None
            or column.onupdate is not None
            or isinstance(getattr(column.server_default, "arg", None), FunctionElement)
        )
        result[attr.key] = ModelField(
            attr.key,
            column,
            annotation,
            not column.nullable and column.default is None and column.server_default is None,
            readonly,
            column.info.get("obox", {}).get("expose", True) is not False,
        )
    return result


def normalize(refs: FieldRefs, model: type[Any], available: Mapping[str, Any]) -> tuple[str, ...]:
    if isinstance(refs, str):
        raise ImproperlyConfigured("Field projections must be sequences, not strings.")
    result = []
    for ref in refs:
        name = ref if isinstance(ref, str) else getattr(ref, "key", None)
        if not isinstance(ref, str) and getattr(ref, "class_", None) is not model:
            raise ImproperlyConfigured("Mapped field references must belong to this model.")
        if not isinstance(name, str) or name not in available or name in result:
            raise ImproperlyConfigured(f"Unknown, unsupported or duplicate field: {name!r}.")
        result.append(name)
    return tuple(result)


def schema(
    name: str,
    specs: Mapping[str, ModelField],
    fields: Iterable[str],
    *,
    partial: bool = False,
    conditional: bool = False,
) -> type[BaseModel]:
    definitions: dict[str, Any] = {}
    for key in fields:
        spec = specs[key]
        default = None if partial or conditional or not spec.required else ...
        definitions[key] = (spec.annotation, default)
    return create_model(name, __config__=ConfigDict(extra="forbid"), **definitions)


def validate(model: type[BaseModel], data: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return model.model_validate(dict(data)).model_dump(exclude_unset=True)
    except ValidationError as exc:
        errors: dict[str, list[str]] = {}
        for error in exc.errors(include_input=False, include_context=False):
            key = ".".join(str(part) for part in error["loc"]) or "__all__"
            errors.setdefault(key, []).append(error["msg"])
        raise BuilderValidationError(errors) from exc
