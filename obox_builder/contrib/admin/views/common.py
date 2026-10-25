"""Shared helpers for admin views."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import Request

from obox_builder.conf import settings
from obox_builder.contrib.admin.navigation import build_navigation
from obox_builder.contrib.auth.csrf import get_csrf_token, validate_csrf
from obox_builder.contrib.auth.models import AnonymousUser, User
from obox_builder.contrib.i18n import get_language_choices
from obox_builder.core.exceptions import ImproperlyConfigured
from obox_builder.http.messages import add_message, get_and_clear_messages

if TYPE_CHECKING:
    from obox_builder.contrib.admin.site import AdminSite

PAGE_SIZE = 20


def current_user(request: Request) -> User | AnonymousUser:
    return getattr(request.state, "user", None) or AnonymousUser()


def csrf_token_for(request: Request) -> str:
    """The session-bound CSRF token (empty when there is no session)."""
    session = getattr(request.state, "auth_session", None)
    return get_csrf_token(session) if session is not None else ""


async def ensure_csrf(request: Request, provided: str | None) -> None:
    """Validate the CSRF token from the form field, falling back to the header.

    ``provided`` is ``None`` when the request carried no form field, which lets
    ``validate_csrf`` fall back to the ``X-CSRF-Token`` header.
    """
    session = getattr(request.state, "auth_session", None)
    if session is None:
        raise ImproperlyConfigured("CSRF validation requires an authenticated session.")
    validate_csrf(request, session, provided or None)


async def flash(request: Request, message: str, level: str = "success") -> None:
    await add_message(request, message, level)


def current_site(request: Request) -> AdminSite:
    """The AdminSite bound to this router, with the global site as fallback."""
    from obox_builder.contrib.admin.site import site as default_site

    return getattr(request.state, "admin_site", None) or default_site


async def page_context(request: Request, **extra: Any) -> dict[str, Any]:
    """Base template context: navigation, flash messages, CSRF and page extras."""
    admin_site = current_site(request)
    user = current_user(request)
    context: dict[str, Any] = {
        "nav_groups": build_navigation(user, admin_site.get_nav_groups()),
        "site_name": admin_site.site_header,
        "site_header": admin_site.site_header,
        "site_title": admin_site.site_title,
        "index_title": admin_site.index_title,
        "language_code": getattr(request.state, "language_code", settings.LANGUAGE_CODE),
        "available_languages": get_language_choices(),
        "current_path": request.url.path,
        "csrf_token": csrf_token_for(request),
        "messages": (
            []
            if request.headers.get("hx-preloaded") == "true"
            else await get_and_clear_messages(request)
        ),
    }
    admin = extra.get("admin")
    if admin is not None:
        context["admin_css"] = admin.extra_css
        context["admin_js"] = admin.extra_js
    context.update(extra)
    return context
