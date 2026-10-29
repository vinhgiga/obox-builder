"""Registry of field types: register once, resolve by name everywhere else."""

from __future__ import annotations

from typing import Any

from obox_builder.exceptions import ImproperlyConfigured
from obox_builder.fields.base import FieldType


class FieldTypes:
    def __init__(self) -> None:
        self._types: dict[str, type[FieldType]] = {}

    def register(self, field_type: type[FieldType], *, replace: bool = False) -> type[FieldType]:
        name = field_type.name
        if not name:
            raise ImproperlyConfigured("FieldType subclasses must define a name.")
        if name in self._types and not replace:
            raise ImproperlyConfigured(
                f"Field type {name!r} is already registered "
                f"({self._types[name].__module__}.{self._types[name].__name__})."
            )
        self._types[name] = field_type
        return field_type

    def resolve(self, name: str) -> type[FieldType]:
        try:
            return self._types[name]
        except KeyError:
            known = ", ".join(sorted(self._types)) or "(none)"
            raise LookupError(f"Unknown field type {name!r}. Registered types: {known}.") from None

    def create(self, name: str, **options: Any) -> FieldType:
        return self.resolve(name)(**options)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._types))

    def _reset(self) -> None:
        self._types.clear()


field_types = FieldTypes()
