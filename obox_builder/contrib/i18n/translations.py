"""Gettext catalog loading with request-local language selection."""

from __future__ import annotations

import gettext as gettext_module
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from obox_builder.conf import settings
from obox_builder.utils.locale import (
    activate_locale,
    get_active_locale,
    normalize_language_tag,
    reset_locale,
)

LANGUAGES = (("en", "English"), ("vi", "Tiếng Việt"))
_BUILTIN_LOCALE_DIR = Path(__file__).parent / "locale"


def _normalize_language(language: str | None) -> str | None:
    if not language:
        return None
    try:
        return get_supported_language_variant(language)
    except LookupError:
        try:
            normalized = normalize_language_tag(language)
        except ValueError:
            return None
        return normalized if normalized == settings.LANGUAGE_CODE else None


def get_language_choices() -> tuple[tuple[str, str], ...]:
    """Return the configured language choices, in display order."""
    return tuple(settings.LANGUAGES)


def get_supported_language_variant(
    language: str,
    *,
    strict: bool = False,
    languages: Sequence[tuple[str, str]] | None = None,
) -> str:
    """Resolve a language tag to a configured language, preferring its base tag."""
    try:
        normalized = normalize_language_tag(language)
    except ValueError as exc:
        raise LookupError(language) from exc
    choices = languages if languages is not None else get_language_choices()
    supported = tuple(normalize_language_tag(code) for code, _ in choices)
    parts = normalized.split("-")
    candidates = tuple("-".join(parts[:size]) for size in range(len(parts), 0, -1))
    for candidate in candidates:
        if candidate in supported:
            return candidate
    if not strict:
        base = candidates[-1]
        for candidate in supported:
            if candidate.startswith(f"{base}-"):
                return candidate
    raise LookupError(language)


def get_language() -> str:
    """Return the active locale or the configured default."""
    return get_active_locale(settings.LANGUAGE_CODE) or settings.LANGUAGE_CODE


@contextmanager
def override(language: str) -> Iterator[None]:
    """Temporarily activate a supported language in the current async context."""
    normalized = _normalize_language(language)
    if normalized is None:
        raise ValueError(f"Unsupported language: {language!r}")
    token = activate_locale(normalized)
    try:
        yield
    finally:
        reset_locale(token)


def _locale_directories() -> tuple[str, ...]:
    from obox_builder.apps import apps

    directories = [str(path) for path in settings.LOCALE_PATHS]
    if apps.ready:
        directories.extend(str(config.path / "locale") for config in apps.get_app_configs())
    directories.append(str(_BUILTIN_LOCALE_DIR))
    return tuple(dict.fromkeys(directories))


@lru_cache(maxsize=32)
def _translation(language: str, directories: tuple[str, ...]) -> gettext_module.NullTranslations:
    catalog: gettext_module.NullTranslations = gettext_module.NullTranslations()
    for directory in reversed(directories):
        path = Path(directory) / language / "LC_MESSAGES" / "messages.mo"
        if not path.is_file():
            continue
        with path.open("rb") as file:
            current = gettext_module.GNUTranslations(file)
        current.add_fallback(catalog)
        catalog = current
    return catalog


def _active_translation() -> gettext_module.NullTranslations:
    language = _normalize_language(get_language()) or "en"
    return _translation(language, _locale_directories())


def gettext(message: str) -> str:
    """Translate a singular message in the active language."""
    return _active_translation().gettext(message)


def ngettext(singular: str, plural: str, number: int) -> str:
    """Translate a singular or plural message in the active language."""
    return _active_translation().ngettext(singular, plural, number)


def pgettext(context: str, message: str) -> str:
    """Translate a context-sensitive message in the active language."""
    return _active_translation().pgettext(context, message)


def npgettext(context: str, singular: str, plural: str, number: int) -> str:
    """Translate a context-sensitive singular or plural message."""
    return _active_translation().npgettext(context, singular, plural, number)


def clear_translation_cache() -> None:
    """Clear loaded catalogs, for applications that refresh translations at runtime."""
    _translation.cache_clear()


__all__ = [
    "LANGUAGES",
    "clear_translation_cache",
    "gettext",
    "get_language",
    "get_language_choices",
    "get_supported_language_variant",
    "ngettext",
    "npgettext",
    "override",
    "pgettext",
]
