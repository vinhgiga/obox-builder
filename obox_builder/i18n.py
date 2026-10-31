"""Locale-prefixed routing and gettext catalogs for the admin chrome."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import cache
from gettext import NullTranslations
from pathlib import Path
from typing import Any

from babel import negotiate_locale
from babel.support import Translations
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

LANGUAGES = {"en": "English", "vi": "Tiếng Việt"}
DEFAULT_LANGUAGE = "en"
DOMAIN = "obox"
LOCALE_DIR = Path(__file__).with_name("locale")


@cache
def catalog(language: str) -> NullTranslations:
    """Load the compiled catalog once per language."""
    return Translations.load(LOCALE_DIR, [language], domain=DOMAIN)


def preferred_languages(header: str) -> list[str]:
    weighted: list[tuple[float, str]] = []
    for item in header.split(","):
        language, *params = (part.strip() for part in item.split(";"))
        try:
            quality = next(float(param[2:]) for param in params if param.startswith("q="))
        except (StopIteration, ValueError):
            quality = 1.0
        if language and quality > 0:
            weighted.append((quality, language))
    weighted.sort(key=lambda entry: entry[0], reverse=True)
    return [language for _, language in weighted]


def negotiate(header: str) -> str:
    match = negotiate_locale(preferred_languages(header), LANGUAGES, sep="-")
    return match.lower() if match and match.lower() in LANGUAGES else DEFAULT_LANGUAGE


def resolve_language(request: Request) -> str:
    return getattr(request.state, "language", DEFAULT_LANGUAGE)


def translations(request: Request) -> NullTranslations:
    return catalog(resolve_language(request))


def _path_parts(request: Request, path: str) -> tuple[str, str]:
    root = request.scope.get("root_path", "")
    return root, path[len(root) :] if root and path.startswith(root) else path


def localize(request: Request, path: str) -> str:
    """Prefix an application path with the current language."""
    root, path = _path_parts(request, path)
    return f"{root}/{resolve_language(request)}{path}"


def translate(request: Request, text: str, **values: Any) -> str:
    message = translations(request).gettext(text)
    return message % values if values else message


def language_url(request: Request, code: str) -> str:
    root, path = _path_parts(request, request.scope.get("obox_path", request.url.path))
    query = request.url.query
    return f"{root}/{code}{path}" + (f"?{query}" if query else "")


def context(request: Request) -> dict[str, Any]:
    messages = translations(request)
    return {
        "language": resolve_language(request),
        "languages": LANGUAGES,
        "language_url": lambda code: language_url(request, code),
        "gettext": messages.gettext,
        "ngettext": messages.ngettext,
        "pgettext": messages.pgettext,
        "npgettext": messages.npgettext,
    }


class LanguageMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        first, _, rest = request.scope["path"].removeprefix("/").partition("/")
        if first in LANGUAGES:
            language = first
            request.scope["obox_path"] = "/" + rest if rest else "/"
            request.scope["path"] = request.scope["obox_path"]
            request.scope["raw_path"] = request.scope["path"].encode()
        else:
            language = negotiate(request.headers.get("accept-language", ""))
            request.scope["obox_path"] = request.scope["path"]
        request.state.language = language
        return await call_next(request)
