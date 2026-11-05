"""Compile scalar SQLAlchemy metadata into explicit Pydantic projections."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from enum import Enum
from typing import Annotated, Any
from uuid import UUID

import sqlalchemy as sa
from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    create_model,
)
from sqlalchemy.exc import NoInspectionAvailable
from sqlalchemy.orm import Mapper
from sqlalchemy.sql.functions import FunctionElement

from obox_builder.errors import BuilderValidationError, ImproperlyConfigured

FieldRefs = Iterable[Any]

FILTER_EMPTY_OPERATORS = frozenset({"is_empty", "is_not_empty"})
FILTER_OPERATORS = frozenset(
    {"is", "is_not", "contains", "does_not_contain", *FILTER_EMPTY_OPERATORS}
)


@dataclass(frozen=True)
class ModelField:
    name: str
    column: Any
    annotation: Any
    required: bool
    readonly: bool
    exposed: bool

    def serialize(self, value: Any) -> Any:
        if "obox_files" in self.column.info and value is not None:
            return [str(getattr(file, "id", file)) for file in value]
        return value

    def normalize(self, value: Any) -> Any:
        if isinstance(value, datetime) and isinstance(self.column.type, sa.DateTime):
            value = value.astimezone(UTC)
            if not self.column.type.timezone:
                value = value.replace(tzinfo=None)
        return value


def _validate_text(value: str) -> str:
    if "\x00" in value:
        raise ValueError("NUL characters are not permitted.")
    if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise ValueError("Unicode surrogate characters are not permitted.")
    return value


def _constrained_annotation(column: sa.Column[Any], annotation: Any) -> Any:
    column_type = column.type
    if isinstance(column_type, sa.DateTime):
        return AwareDatetime
    if isinstance(column_type, sa.BigInteger):
        return Annotated[int, Field(ge=-(2**63), le=2**63 - 1)]
    if isinstance(column_type, sa.Integer):
        bounds = (
            (-(2**15), 2**15 - 1)
            if isinstance(column_type, sa.SmallInteger)
            else (-(2**31), 2**31 - 1)
        )
        return Annotated[int, Field(ge=bounds[0], le=bounds[1])]
    if isinstance(column_type, sa.Numeric):
        constraints: dict[str, Any] = {}
        if column_type.precision is not None:
            constraints["max_digits"] = column_type.precision
        if column_type.scale is not None:
            constraints["decimal_places"] = column_type.scale
        return Annotated[Decimal, Field(**constraints)] if constraints else annotation
    if annotation is str and isinstance(column_type, sa.String):
        if column_type.length:
            return Annotated[
                str, Field(max_length=column_type.length), AfterValidator(_validate_text)
            ]
        return Annotated[str, AfterValidator(_validate_text)]
    return annotation


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
        annotation = _constrained_annotation(column, annotation)
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
    for relation in mapper.relationships:
        rule = relation.info.get("obox_files")
        if rule is not None:
            column = sa.Column(
                relation.key, sa.JSON, nullable=False, default=list, info={"obox_files": rule}
            )
            result[relation.key] = ModelField(
                relation.key,
                column,
                Annotated[list[str], Field(min_length=rule.min_count, max_length=rule.max_count)],
                bool(rule.min_count),
                False,
                True,
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
    base: type[BaseModel] = BaseModel,
) -> type[BaseModel]:
    definitions: dict[str, Any] = {}
    for key in fields:
        spec = specs[key]
        default = None if partial or conditional or not spec.required else ...
        annotation = (
            (datetime | None if spec.column.nullable else datetime)
            if getattr(base, "model_config", {}).get("from_attributes")
            and isinstance(spec.column.type, sa.DateTime)
            else spec.annotation
        )
        definitions[key] = (annotation, default)
    return create_model(name, __base__=base, __config__=ConfigDict(extra="forbid"), **definitions)


def validate(model: type[BaseModel], data: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return model.model_validate(dict(data)).model_dump(exclude_unset=True)
    except ValidationError as exc:
        errors: dict[str, list[str]] = {}
        for error in exc.errors(include_input=False, include_context=False):
            key = ".".join(str(part) for part in error["loc"]) or "__all__"
            errors.setdefault(key, []).append(error["msg"])
        raise BuilderValidationError(errors, field_code="invalid_type") from exc
