"""Language preference endpoint."""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, Form, Request, Response
from starlette.responses import RedirectResponse

from obox_builder.conf import settings
from obox_builder.contrib.i18n.translations import (
    get_language_choices,
    get_supported_language_variant,
)
from obox_builder.contrib.i18n.urls import get_language_from_path
from obox_builder.http.urls import safe_next_url

router = APIRouter(prefix="/i18n", tags=["i18n"])


def _origin(url: str, *, bare: bool = False) -> tuple[str, str, int] | None:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if (
        scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or (bare and (parts.path or parts.query or parts.fragment))
    ):
        return None
    if port is None:
        port = 443 if scheme == "https" else 80
    return scheme, parts.hostname.lower(), port


def _same_origin_request(request: Request) -> bool:
    origin = request.headers.get("origin")
    if origin is not None:
        source = _origin(origin, bare=True)
        return source is not None and source == _origin(str(request.url))
    if request.url.scheme == "https":
        referer = request.headers.get("referer")
        return bool(referer and _origin(referer) == _origin(str(request.url)))
    return True


def _localized_next_url(request: Request, target: str, language: str) -> str:
    """Apply the selected locale prefix when the URLconf uses i18n patterns."""
    app_state = request.app.state
    if not getattr(app_state, "i18n_patterns_used", False):
        return target

    parts = urlsplit(target)
    path = parts.path
    current_language = get_language_from_path(path, languages=get_language_choices())
    if current_language:
        path = path[len(f"/{current_language}") :]
    prefix_default_language = getattr(app_state, "i18n_prefix_default_language", True)
    default_language = get_supported_language_variant(settings.LANGUAGE_CODE)
    if prefix_default_language or language != default_language:
        path = f"/{language}{path}"
    return urlunsplit(parts._replace(path=path))


@router.post("/set-language", name="set_language")
async def set_language(
    request: Request,
    language: str = Form(...),
    next: str = Form(""),
) -> Response:
    """Store a supported language preference and return to a safe local path."""
    if not _same_origin_request(request):
        return Response(status_code=403)
    try:
        language = get_supported_language_variant(language)
    except LookupError:
        return Response(status_code=400)

    target = _localized_next_url(request, safe_next_url(next), language)
    fragment_target = re.sub(
        r"^[A-Za-z][\w-]*(?=#[\w-]+$)", "", request.headers.get("hx-target", "")
    )
    if (
        request.headers.get("hx-request") == "true"
        and fragment_target
        and request.headers.get("hx-request-type") != "full"
    ):
        response: Response = Response(
            status_code=200,
            headers={
                "HX-Location": json.dumps(
                    {
                        "path": target,
                        "target": fragment_target,
                        "swap": "outerHTML",
                        "push": target,
                    }
                )
            },
        )
    else:
        response = RedirectResponse(target, status_code=303)
    response.set_cookie(
        settings.LANGUAGE_COOKIE_NAME,
        language,
        max_age=settings.LANGUAGE_COOKIE_AGE,
        path="/",
        secure=settings.SESSION_COOKIE_SECURE and not settings.DEBUG,
        httponly=True,
        samesite="lax",
    )
    return response


__all__ = ["router", "set_language"]
