"""Locale-prefixed routing and gettext catalogs for the admin chrome."""

from __future__ import annotations

from collections.abc import Sequence
from functools import cache
from gettext import NullTranslations
from pathlib import Path
from typing import Any

from babel import negotiate_locale
from babel.support import Translations
from fastapi.routing import RouteContext
from starlette._utils import get_route_path
from starlette.requests import Request
from starlette.responses import RedirectResponse
from starlette.routing import BaseRoute, Match
from starlette.types import ASGIApp, Receive, Scope, Send

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
    if root and (path == root or path.startswith(root + "/")):
        path = path[len(root) :]
    return root, path


def localize(request: Request, path: str, *, rooted: bool = False) -> str:
    """Prefix an application path with the current language."""
    root = request.scope.get("root_path", "")
    if rooted:
        _, path = _path_parts(request, path)
    language_prefix = request.scope.get("obox_language_prefix", resolve_language(request))
    if language_prefix is None:
        return f"{root}{path}"
    return f"{root}/{language_prefix}{path}"


def request_target(request: Request) -> str:
    """Return the current request path and query with its original locale prefix."""
    path = request.scope.get("obox_path")
    if not isinstance(path, str):
        path = request.url.path
    root, path = _path_parts(request, path)
    language_prefix = request.scope.get("obox_language_prefix")
    if isinstance(language_prefix, str):
        path = f"/{language_prefix}{path}"
    target = f"{root}{path}"
    query = request.url.query
    return f"{target}?{query}" if query else target


def translate(request: Request, text: str, **values: Any) -> str:
    message = translations(request).gettext(text)
    return message % values if values else message


def language_url(request: Request, code: str) -> str:
    root = request.scope.get("root_path", "")
    path = request.scope.get("obox_path")
    if path is None:
        _, path = _path_parts(request, request.url.path)
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


class LanguageMiddleware:
    def __init__(
        self, app: ASGIApp, *, prefixes: Sequence[str], routes: Sequence[BaseRoute | RouteContext]
    ) -> None:
        self.app, self.prefixes, self.routes = app, prefixes, routes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = get_route_path(scope)
        first, _, rest = path.removeprefix("/").partition("/")
        localized = first in LANGUAGES
        target = "/" + rest if localized else path
        if not any(target == p or target.startswith(p + "/") for p in self.prefixes):
            await self.app(scope, receive, send)
            return
        scope = dict(scope)
        root = scope.get("root_path", "")
        target_scope = {**scope, "path": root + target}
        if not any(route.matches(target_scope)[0] != Match.NONE for route in self.routes):
            redirect_target = target.rstrip("/") if target.endswith("/") else target + "/"
            target_scope["path"] = root + redirect_target
            if localized and any(
                route.matches(target_scope)[0] != Match.NONE for route in self.routes
            ):
                url = Request(scope).url.replace(path=f"{root}/{first}{redirect_target}")
                await RedirectResponse(str(url))(scope, receive, send)
                return
            await self.app(scope, receive, send)
            return
        scope["state"] = dict(scope.get("state", {}))
        scope["obox_language_prefix"] = first if localized else None
        request = Request(scope)
        request.state.language = (
            first if localized else negotiate(request.headers.get("accept-language", ""))
        )
        scope["obox_path"] = target
        if localized:
            scope["path"] = root + target
            scope["raw_path"] = scope["path"].encode("utf-8")
        await self.app(scope, receive, send)
