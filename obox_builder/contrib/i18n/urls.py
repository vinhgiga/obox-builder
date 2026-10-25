"""Language-prefixed router helpers, similar to Django's ``i18n_patterns``."""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from fastapi import APIRouter

from obox_builder.contrib.i18n.translations import get_supported_language_variant
from obox_builder.utils.locale import normalize_language_tag

RouterSpec = APIRouter | tuple[APIRouter, str]


@dataclass(frozen=True)
class I18nPatterns:
    """A group of routers mounted beneath configured language URL prefixes."""

    routers: tuple[tuple[APIRouter, str], ...]
    prefix_default_language: bool = True

    def iter_router_prefixes(
        self,
        languages: Sequence[tuple[str, str]],
        default_language: str,
    ) -> Iterator[tuple[APIRouter, str]]:
        """Yield each router once per language with its effective URL prefix."""
        try:
            default = get_supported_language_variant(default_language, languages=languages)
        except LookupError:
            default = normalize_language_tag(default_language)
        for router, base_prefix in self.routers:
            for code, _ in languages:
                language = normalize_language_tag(code)
                prefix = base_prefix
                if self.prefix_default_language or language != default:
                    prefix = f"/{language}{prefix}"
                yield router, prefix


def i18n_patterns(
    *routers: RouterSpec,
    prefix_default_language: bool = True,
) -> I18nPatterns:
    """Mount routers under locale prefixes, optionally leaving the default unprefixed.

    Pass either ``APIRouter`` instances or ``(router, prefix)`` pairs, as in the
    project's root ``routers`` list.
    """
    entries: list[tuple[APIRouter, str]] = []
    for entry in routers:
        if isinstance(entry, APIRouter):
            entries.append((entry, ""))
        elif (
            isinstance(entry, tuple)
            and len(entry) == 2
            and isinstance(entry[0], APIRouter)
            and isinstance(entry[1], str)
        ):
            entries.append(entry)
        else:
            raise TypeError(
                "i18n_patterns entries must be APIRouter instances or (router, prefix) pairs."
            )
    return I18nPatterns(tuple(entries), prefix_default_language)


_LANGUAGE_PREFIX = re.compile(r"^/([^/]+)(?:/|$)")


def get_language_from_path(
    path: str,
    *,
    languages: Sequence[tuple[str, str]] | None = None,
) -> str | None:
    """Return a configured language from a URL prefix, if one is present."""
    match = _LANGUAGE_PREFIX.match(path)
    if match is None:
        return None
    try:
        return get_supported_language_variant(match.group(1), languages=languages)
    except LookupError:
        return None


__all__ = ["I18nPatterns", "get_language_from_path", "i18n_patterns"]
