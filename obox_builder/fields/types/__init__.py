"""Built-in field types; importing this package registers them."""

from __future__ import annotations

from obox_builder.fields.registry import field_types
from obox_builder.fields.types.boolean import Checkbox
from obox_builder.fields.types.choice import SingleSelect
from obox_builder.fields.types.json import Json
from obox_builder.fields.types.number import Decimal, Integer
from obox_builder.fields.types.relation import Link, MultiLink
from obox_builder.fields.types.temporal import Date, DateTime, Time
from obox_builder.fields.types.text import URL, Email, LongText, Phone, Text

BUILTIN_TYPES: tuple[type, ...] = (
    Text,
    LongText,
    Email,
    URL,
    Phone,
    Integer,
    Decimal,
    Checkbox,
    Date,
    DateTime,
    Time,
    SingleSelect,
    Link,
    MultiLink,
    Json,
)

for _type in BUILTIN_TYPES:
    field_types.register(_type, replace=True)

__all__ = [
    "BUILTIN_TYPES",
    "Checkbox",
    "Date",
    "DateTime",
    "Decimal",
    "Email",
    "Integer",
    "Json",
    "Link",
    "MultiLink",
    "LongText",
    "Phone",
    "SingleSelect",
    "Text",
    "Time",
    "URL",
]
