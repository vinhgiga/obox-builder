"""Field-type system: the core of obox-builder's admin.

``FieldType`` is a Strategy: it knows how one kind of data travels through the
whole system (validate, read input, render a cell, filter, search, history).
Adding a type means adding one class, not touching views, templates or exporters.

This module is deliberately free of HTTP and Jinja imports.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Any, ClassVar
from uuid import UUID

from sqlalchemy import String
from sqlalchemy.sql.elements import ColumnElement

__all__ = [
    "Cell",
    "FieldFilterError",
    "FieldSpec",
    "FieldType",
    "Operator",
    "RecordContext",
]


class FieldError(Exception):
    """Base class for field-level errors raised outside validation."""


class FieldFilterError(FieldError):
    """A filter could not be built (unknown field, operator or bad value)."""


class FieldSearchError(FieldError):
    """A search term could not be applied to a column."""


@dataclass(frozen=True)
class Operator:
    """A filter operator supported by a :class:`FieldType`."""

    name: str
    label: str
    arity: int = 1


@dataclass
class RecordContext:
    """Everything a field callable (``show_if``, ``initial``, ...) may inspect.

    ``values`` holds the *draft* values while a form is being filled in, so
    server-side conditions see the data the user has typed, not just the
    persisted ``obj``.
    """

    obj: Any = None
    values: Mapping[str, Any] = field(default_factory=dict)
    user: Any = None
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Cell:
    """A rendered list cell (presentation only; templates decide markup)."""

    text: str = ""
    kind: str = "text"
    url: str | None = None
    value: Any = None


@dataclass(frozen=True)
class FieldSpec:
    """Declarative description of one model field as seen by the admin."""

    name: str
    label: str
    type: FieldType
    column: ColumnElement[Any] | None = None
    nullable: bool = True
    required: bool = False
    readonly: bool = False
    editable: bool = True
    history: bool = True
    primary_key: bool = False
    help_text: str = ""
    options: Mapping[str, Any] = field(default_factory=dict)
    show_if: Any = None
    editable_if: Any = None
    required_if: Any = None
    valid_if: Any = None
    initial: Any = None

    @property
    def secret(self) -> bool:
        return bool(self.options.get("secret", False))

    def is_editable(self) -> bool:
        return self.editable and not self.readonly and not self.secret


class FieldType(ABC):
    """Strategy: one data type across form, list, filter, search and history."""

    name: ClassVar[str]
    widget: ClassVar[str] = "text"
    sortable: ClassVar[bool] = True
    searchable: ClassVar[bool] = False
    operators: ClassVar[Mapping[str, Operator]] = {}

    def __init__(self, **options: Any) -> None:
        self.options: dict[str, Any] = dict(options)

    def __repr__(self) -> str:
        if self.options:
            return f"<{type(self).__name__} {self.options!r}>"
        return f"<{type(self).__name__}>"

    # -- validation ---------------------------------------------------------
    @abstractmethod
    def annotation(self, spec: FieldSpec) -> Any:
        """Return the Python/Pydantic annotation used to validate this field."""

    def output_annotation(self, spec: FieldSpec) -> Any:
        from dataclasses import replace

        return self.annotation(replace(spec, required=True))

    def from_input(self, raw: Any, spec: FieldSpec) -> Any:
        """Normalise a raw form value before validation."""
        return raw

    def from_partial_input(self, raw: Any, spec: FieldSpec) -> Any:
        """Normalise a partial filter value without changing its meaning."""
        return self.from_input(raw, spec)

    def from_api(self, raw: Any, spec: FieldSpec) -> Any:
        """Decode a JSON value; custom types may override this boundary."""
        return self.from_input(raw, spec)

    def to_api(self, value: Any, spec: FieldSpec) -> Any:
        """Encode scalar values without losing decimal or integer precision."""
        if value is None:
            return None
        if isinstance(value, datetime):
            return (
                value.replace(tzinfo=value.tzinfo or timezone.utc)
                .astimezone(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )
        if isinstance(value, (date, time)):
            return value.isoformat()
        if isinstance(value, (Decimal, UUID)) or self.name == "link" or spec.primary_key:
            return str(value)
        if spec.column is not None:
            from sqlalchemy import BigInteger

            if isinstance(spec.column.type, BigInteger):
                return str(value)
        return self.diff_repr(value)

    def validate(self, value: Any, spec: FieldSpec) -> str | None:
        """Type-level validation that runs before Pydantic; return an error."""
        return None

    # -- presentation -------------------------------------------------------
    def to_input(self, value: Any, spec: FieldSpec) -> str:
        """Render a value into an HTML input attribute."""
        return "" if value is None else str(value)

    def cell(self, value: Any, spec: FieldSpec) -> Cell:
        """Render a value for the list view."""
        return Cell(
            text="" if value is None else str(value),
            value=value,
        )

    # -- history ------------------------------------------------------------
    def diff_repr(self, value: Any) -> Any:
        """Stable JSON representation used by the change log."""
        return value

    # -- filtering and search ----------------------------------------------
    def build_filter(self, column: ColumnElement[Any], op: str, value: Any) -> ColumnElement[bool]:
        if op not in self.operators:
            raise FieldFilterError(f"Operator {op!r} is not supported for this field.")
        try:
            return self._build(column, op, value)
        except FieldFilterError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            raise FieldFilterError(f"Invalid filter value ({exc}).") from exc

    def _build(self, column: ColumnElement[Any], op: str, value: Any) -> ColumnElement[bool]:
        if op == "isnull":
            return column.is_(None) if value else column.is_not(None)
        if op == "eq":
            return column == value
        if op == "ne":
            return column != value
        if op == "gt":
            return column > value
        if op == "gte":
            return column >= value
        if op == "lt":
            return column < value
        if op == "lte":
            return column <= value
        if op == "in":
            values = value if isinstance(value, (list, tuple, set)) else [value]
            return column.in_(list(values))
        if op == "contains":
            return self._like(column, f"%{like_escape(value)}%")
        if op == "startswith":
            return self._like(column, f"{like_escape(value)}%")
        if op == "endswith":
            return self._like(column, f"%{like_escape(value)}")
        raise FieldFilterError(f"Unknown operator {op!r}.")

    @staticmethod
    def _like(column: ColumnElement[Any], pattern: str) -> ColumnElement[bool]:
        if isinstance(column.type, String):
            return column.ilike(pattern, escape="\\")
        raise FieldFilterError("Text matching requires a text column.")

    def build_search(self, column: ColumnElement[Any], term: str) -> ColumnElement[bool]:
        if not self.searchable:
            raise FieldSearchError(f"{self.name} fields cannot be searched.")
        from obox_builder.db.search import SearchText, fold_text

        return SearchText(column).like(f"%{like_escape(fold_text(term))}%", escape="\\")


def like_escape(value: Any) -> str:
    """Escape LIKE wildcards in a user-provided search/filter value."""
    return str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
