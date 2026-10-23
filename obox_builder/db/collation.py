"""Vietnamese ICU ordering evaluated by the database before pagination."""

from __future__ import annotations

from collections.abc import Callable
from importlib import import_module
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.functions import FunctionElement

from obox_builder.core.exceptions import ImproperlyConfigured


def _icu() -> Any:
    try:
        return import_module("icu")
    except ImportError as exc:
        raise ImproperlyConfigured(
            "Vietnamese ordering on SQLite requires obox-builder[vietnamese] "
            "and the system ICU libraries."
        ) from exc


def sqlite_sort_key() -> Callable[[str | None], bytes | None]:
    # Each SQLite connection owns its collator; mutable ICU objects are not shared.
    collator: Any = None

    def key(value: str | None) -> bytes | None:
        nonlocal collator
        if value is None:
            return None
        if collator is None:
            icu = _icu()
            collator = icu.Collator.createInstance(icu.Locale("vi"))
            collator.setStrength(icu.Collator.TERTIARY)
            collator.setAttribute(icu.UCollAttribute.NORMALIZATION_MODE, icu.UCollAttributeValue.ON)
        # ICU keys terminate with NUL. Append a bytewise tie-breaker to match
        # PostgreSQL's deterministic collation and keep distinct groups contiguous.
        return bytes(collator.getSortKey(value)) + value.encode("utf-8")

    return key


class VietnameseOrder(FunctionElement[Any]):
    type = sa.String()
    inherit_cache = True


@compiles(VietnameseOrder, "sqlite")
def _compile_sqlite(element: VietnameseOrder, compiler: Any, **kw: Any) -> str:
    _icu()
    return f"obox_vi_sort_key({compiler.process(element.clauses, **kw)})"


@compiles(VietnameseOrder, "postgresql")
def _compile_postgres(element: VietnameseOrder, compiler: Any, **kw: Any) -> str:
    return f'({compiler.process(element.clauses, **kw)} COLLATE pg_catalog."vi-x-icu")'
