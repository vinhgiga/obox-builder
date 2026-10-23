"""Accent folding shared by SQLite connections and PostgreSQL expressions."""

from __future__ import annotations

import unicodedata
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.functions import FunctionElement

# Precomposed Vietnamese letters plus their decomposed tone/vowel marks.
_LETTERS = "àáảãạăằắẳẵặâầấẩẫậèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợùúủũụưừứửữựỳýỷỹỵđ"
_MARKS = "\u0300\u0301\u0303\u0309\u0323\u0302\u0306\u031b"
_SOURCE = _LETTERS + _LETTERS.upper() + _MARKS


def fold_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = unicodedata.normalize("NFD", value).replace("đ", "d").replace("Đ", "D")
    return "".join(char for char in text if not unicodedata.combining(char)).lower()


_TARGET = "".join(fold_text(char) or "" for char in _LETTERS + _LETTERS.upper())


class SearchText(FunctionElement[str]):
    """Case and Vietnamese accent folding without a database extension."""

    type = sa.String()
    name = "obox_search_text"
    inherit_cache = True


@compiles(SearchText)
def _compile_default(element: SearchText, compiler: Any, **kw: Any) -> str:
    return f"obox_search_text({compiler.process(element.clauses, **kw)})"


@compiles(SearchText, "postgresql")
def _compile_postgres(element: SearchText, compiler: Any, **kw: Any) -> str:
    arg = compiler.process(element.clauses, **kw)
    return f"lower(translate({arg}, '{_SOURCE}', '{_TARGET}'))"
