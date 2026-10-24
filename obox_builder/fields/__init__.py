"""Field-type system: the core of the admin's form, list, filter and history.

Import order matters: ``types`` registers the built-in field types.
"""

from obox_builder.fields import types as _types  # noqa: F401  (registers built-ins)
from obox_builder.fields.base import (
    Cell,
    FieldError,
    FieldFilterError,
    FieldSearchError,
    FieldSpec,
    FieldType,
    Operator,
    RecordContext,
)
from obox_builder.fields.introspect import introspect_model
from obox_builder.fields.query import FilterGroup, FilterTerm, QuerySpec, QuerySpecError, SortTerm
from obox_builder.fields.registry import FieldTypes, field_types
from obox_builder.fields.types.file import File

__all__ = [
    "Cell",
    "FieldError",
    "FieldFilterError",
    "FieldSearchError",
    "FieldSpec",
    "FieldType",
    "File",
    "FieldTypes",
    "FilterGroup",
    "FilterTerm",
    "Operator",
    "QuerySpec",
    "QuerySpecError",
    "RecordContext",
    "SortTerm",
    "field_types",
    "introspect_model",
]
