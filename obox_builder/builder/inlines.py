"""Child collections edited alongside their parent form."""

from __future__ import annotations

from typing import Any, Generic, TypeVar

import sqlalchemy as sa

from obox_builder.errors import ImproperlyConfigured

T = TypeVar("T")


class Inline(Generic[T]):
    model: type[T] | None = None
    fk: str | None = None
    extra = 3
    label: str | None = None
    name: str
    fk_name: str
    relation_name: str
    _parent: Any

    def _compile(self, parent: Any) -> None:
        model = self.model
        if model is None:
            raise ImproperlyConfigured("An inline must declare its model.")
        if isinstance(self.extra, bool) or not isinstance(self.extra, int) or self.extra < 0:
            raise ImproperlyConfigured("Inline.extra must be a non-negative integer.")
        if model is parent.model:
            raise ImproperlyConfigured("An inline cannot target its parent model.")
        tables = sa.inspect(parent.model).tables
        mapper = sa.inspect(model)
        if mapper is None:
            raise ImproperlyConfigured("Register a SQLAlchemy mapped class.")
        candidates = [
            attr.key
            for attr in mapper.column_attrs
            if len(attr.columns) == 1
            and any(fk.column.table in tables for fk in attr.columns[0].foreign_keys)
        ]
        if self.fk is not None:
            if self.fk not in candidates:
                raise ImproperlyConfigured("Inline.fk must reference the parent model.")
            self.fk_name = self.fk
        elif len(candidates) == 1:
            self.fk_name = candidates[0]
        else:
            raise ImproperlyConfigured("An inline needs exactly one foreign key to its parent.")
        fk_column = mapper.columns[self.fk_name]
        relations = [
            relation.key
            for relation in sa.inspect(parent.model).relationships
            if relation.mapper.class_ is model
            and any(remote is fk_column for _, remote in relation.synchronize_pairs)
        ]
        if len(relations) != 1:
            raise ImproperlyConfigured("An inline needs exactly one parent relationship.")
        self.relation_name = relations[0]
        self.name = model.__name__
        self.label = self.label or f"{self.name}s"
        self._parent = parent
