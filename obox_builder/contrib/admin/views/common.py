"""Shared helpers for admin views."""

from __future__ import annotations

from typing import Any

from fastapi import Request

from obox_builder.contrib.admin.navigation import build_navigation
from obox_builder.contrib.auth.csrf import get_csrf_token, validate_csrf
from obox_builder.contrib.auth.models import AnonymousUser, User
from obox_builder.core.exceptions import ImproperlyConfigured
from obox_builder.http.messages import add_message, get_and_clear_messages

PAGE_SIZE = 20


def current_user(request: Request) -> User | AnonymousUser:
    return getattr(request.state, "user", None) or AnonymousUser()


def csrf_token_for(request: Request) -> str:
    """The session-bound CSRF token (empty when there is no session)."""
    session = getattr(request.state, "auth_session", None)
    return get_csrf_token(session) if session is not None else ""


async def ensure_csrf(request: Request, provided: str) -> None:
    session = getattr(request.state, "auth_session", None)
    if session is None:
        raise ImproperlyConfigured("CSRF validation requires an authenticated session.")
    validate_csrf(request, session, provided)


async def flash(request: Request, message: str, level: str = "success") -> None:
    await add_message(request, message, level)


async def page_context(
    request: Request, site_navigation: Any = None, **extra: Any
) -> dict[str, Any]:
    """Base template context: navigation, flash messages, CSRF and page extras."""
    if site_navigation is None:
        from obox_builder.contrib.admin.site import site

        site_navigation = site.navigation
    user = current_user(request)
    context: dict[str, Any] = {
        "nav_groups": build_navigation(user, site_navigation),
        "site_name": "obox admin",
        "current_path": request.url.path,
        "csrf_token": csrf_token_for(request),
        "messages": await get_and_clear_messages(request),
    }
    context.update(extra)
    return context
