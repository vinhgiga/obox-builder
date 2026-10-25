"""Request locale negotiation middleware."""

from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import urlsplit, urlunsplit

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import RedirectResponse
from starlette.routing import Match
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from obox_builder.conf import settings
from obox_builder.contrib.i18n.translations import (
    get_language_choices,
    get_supported_language_variant,
)
from obox_builder.contrib.i18n.urls import get_language_from_path
from obox_builder.utils.locale import activate_locale, reset_locale


def _language_from_range(value: str, languages: Sequence[tuple[str, str]]) -> str | None:
    try:
        return get_supported_language_variant(value, languages=languages)
    except LookupError:
        return None


def _accepted_languages(
    header: str, languages: Sequence[tuple[str, str]]
) -> list[tuple[str, float, int]]:
    accepted: list[tuple[str, float, int]] = []
    for index, item in enumerate(header[:1024].split(",")[:20]):
        language, *parameters = item.split(";")
        language = language.strip()
        quality = 1.0
        for parameter in parameters:
            key, separator, value = parameter.strip().partition("=")
            if key.lower() == "q":
                try:
                    quality = float(value) if separator else 0.0
                except ValueError:
                    quality = 0.0
                break
        if 0 <= quality <= 1 and (language == "*" or _language_from_range(language, languages)):
            accepted.append((language, quality, index))
    return sorted(accepted, key=lambda entry: (-entry[1], entry[2]))


def _route_path(scope: Scope) -> str:
    """Return the path relative to an app mount, as route matching expects."""
    path = scope["path"]
    root_path = scope.get("root_path", "").rstrip("/")
    if root_path and path.startswith(f"{root_path}/"):
        path = path[len(root_path) :]
    elif root_path and path == root_path:
        path = "/"
    return path or "/"


def _localized_route_path(scope: Scope, language: str) -> str:
    root_path = scope.get("root_path", "").rstrip("/")
    return f"{root_path}/{language}{_route_path(scope)}"


def _route_matches(request: Request, path: str) -> bool:
    scope = {**request.scope, "path": path}
    return any(route.matches(scope)[0] is not Match.NONE for route in request.app.router.routes)


def _localized_redirect_path(request: Request, language: str) -> str | None:
    """Return a localized route path when a prefixed route exists."""
    path = _localized_route_path(request.scope, language)
    if _route_matches(request, path):
        return path

    if request.app.router.redirect_slashes:
        alternate = path.rstrip("/") if path.endswith("/") else f"{path}/"
        if _route_matches(request, alternate):
            return alternate
    return None


def select_language(
    cookie_value: str | None,
    accept_language: str | None,
    default: str,
    *,
    languages: Sequence[tuple[str, str]] | None = None,
) -> str:
    """Select a supported language from the preference cookie or request header."""
    languages = tuple(languages) if languages is not None else get_language_choices()
    cookie_language = _language_from_range(cookie_value or "", languages)
    if cookie_language:
        return cookie_language

    accepted = _accepted_languages(accept_language or "", languages)
    excluded = {
        _language_from_range(language, languages)
        for language, quality, _ in accepted
        if quality == 0 and language != "*"
    }
    for language, quality, _ in accepted:
        if quality == 0:
            continue
        if language == "*":
            default_language = _language_from_range(default, languages)
            if default_language and default_language not in excluded:
                return default_language
            for code, _ in languages:
                if code not in excluded:
                    return code
            continue
        supported = _language_from_range(language, languages)
        if supported and supported not in excluded:
            return supported
    if default_language := _language_from_range(default, languages):
        return default_language
    return default


def get_language_from_request(request: Request, *, check_path: bool = False) -> str:
    """Return the locale selected for a Starlette request."""
    languages = get_language_choices()
    if check_path:
        language = get_language_from_path(_route_path(request.scope), languages=languages)
        if language:
            return language
    return select_language(
        request.cookies.get(settings.LANGUAGE_COOKIE_NAME),
        request.headers.get("accept-language"),
        settings.LANGUAGE_CODE,
        languages=languages,
    )


def _localize_admin_redirect(url: str, language: str) -> str:
    """Keep admin redirects beneath the locale prefix of the current request."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if (
        parts.scheme
        or parts.netloc
        or get_language_from_path(parts.path, languages=get_language_choices())
    ):
        return url
    if parts.path != "/admin" and not parts.path.startswith("/admin/"):
        return url
    return urlunsplit(parts._replace(path=f"/{language}{parts.path}"))


class LocaleMiddleware:
    """Install a request-local language and label the response for caches and clients."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        app_state = getattr(scope.get("app"), "state", None)
        i18n_patterns_used = bool(getattr(app_state, "i18n_patterns_used", False))
        prefix_default_language = bool(getattr(app_state, "i18n_prefix_default_language", True))
        path_language = (
            get_language_from_path(_route_path(request.scope), languages=get_language_choices())
            if i18n_patterns_used
            else None
        )
        language = (
            settings.LANGUAGE_CODE
            if i18n_patterns_used and not prefix_default_language and path_language is None
            else get_language_from_request(request, check_path=i18n_patterns_used)
        )
        if (
            request.method in {"GET", "HEAD"}
            and i18n_patterns_used
            and prefix_default_language
            and path_language is None
        ):
            localized_path = _localized_redirect_path(request, language)
            if localized_path:
                target = localized_path
                if request.url.query:
                    target = f"{target}?{request.url.query}"
                response = RedirectResponse(target, status_code=302)
                response.headers["content-language"] = language
                response.headers["vary"] = "Accept-Language, Cookie"
                await response(scope, receive, send)
                return
        scope.setdefault("state", {})["language_code"] = language
        token = activate_locale(language)

        async def send_localized(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["content-language"] = language
                if path_language:
                    for name in ("location", "hx-redirect"):
                        destination = headers.get(name)
                        if destination:
                            headers[name] = _localize_admin_redirect(destination, path_language)
                vary = [
                    value.strip() for value in headers.get("vary", "").split(",") if value.strip()
                ]
                normalized = {value.lower() for value in vary}
                if "*" not in normalized and path_language is None:
                    for value in ("Accept-Language", "Cookie"):
                        if value.lower() not in normalized:
                            vary.append(value)
                    headers["vary"] = ", ".join(vary)
            await send(message)

        try:
            await self.app(scope, receive, send_localized)
        finally:
            reset_locale(token)


__all__ = ["LocaleMiddleware", "get_language_from_request", "select_language"]
