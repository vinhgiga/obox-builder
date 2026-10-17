"""Login, logout and password change views."""

from __future__ import annotations

from fastapi import Depends, Form, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from obox_builder.conf import settings
from obox_builder.contrib.admin.views.common import ensure_csrf, page_context
from obox_builder.contrib.auth import csrf, services, sessions
from obox_builder.contrib.auth.dependencies import get_auth_session, login_required
from obox_builder.contrib.auth.models import Session as AuthSession
from obox_builder.contrib.auth.models import User
from obox_builder.contrib.auth.validators import PasswordValidationError
from obox_builder.db.session import get_db
from obox_builder.http.messages import add_session_message
from obox_builder.http.shortcuts import redirect, render
from obox_builder.http.urls import safe_next_url


async def login_view(
    request: Request,
    auth_session: AuthSession | None = Depends(get_auth_session),
) -> Response:
    """GET /login: redirect authenticated users, otherwise show the form."""
    if auth_session is not None:
        return redirect(settings.LOGIN_REDIRECT_URL, status_code=303)
    context = await page_context(request, next=request.query_params.get("next", ""))
    return render(request, "admin/auth/login.html", context)


async def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    next: str = Form(""),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """POST /login: authenticate, cycle the session cookie, redirect safely."""
    if not csrf.check_origin(request):
        context = await page_context(
            request, next=next, username=username, error="Cross-origin request rejected."
        )
        return render(request, "admin/auth/login.html", context, status_code=403)

    user = await services.authenticate(db, username, password)
    if user is None:
        context = await page_context(
            request, next=next, username=username, error="Invalid username or password."
        )
        return render(request, "admin/auth/login.html", context, status_code=200)

    old_token = request.cookies.get(settings.SESSION_COOKIE_NAME)
    if old_token:
        await sessions.delete_session_by_token(db, old_token)  # session fixation guard
    token, _ = await sessions.create_session(
        db,
        user.id,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    response = redirect(safe_next_url(next), status_code=303)
    sessions.set_session_cookie(response, token)
    return response


async def logout_view(
    request: Request,
    user: User = Depends(login_required),
    csrf_token: str = Form(""),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """POST /logout: validate CSRF, delete the session row and clear the cookie."""
    await ensure_csrf(request, csrf_token)
    auth_session: AuthSession | None = getattr(request.state, "auth_session", None)
    if auth_session is not None:
        await sessions.delete_session(db, auth_session)
    response = redirect(settings.LOGIN_URL, status_code=303)
    sessions.clear_session_cookie(response)
    return response


async def password_change_view(
    request: Request,
    user: User = Depends(login_required),
) -> Response:
    """GET /password_change: show the form."""
    return render(request, "admin/auth/password_change.html", await page_context(request))


async def password_change_submit(
    request: Request,
    user: User = Depends(login_required),
    csrf_token: str = Form(""),
    current_password: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """POST /password_change: change the password and keep the current session alive."""
    await ensure_csrf(request, csrf_token)
    if new_password != confirm_password:
        return render(
            request,
            "admin/auth/password_change.html",
            {"errors": ["The two password fields do not match."]},
            status_code=200,
        )
    try:
        await services.change_password(db, user, current_password, new_password)
    except PasswordValidationError as exc:
        return render(
            request,
            "admin/auth/password_change.html",
            {"errors": exc.errors},
            status_code=200,
        )

    token, new_session = await sessions.create_session(  # change_password revoked every session
        db,
        user.id,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    # The old session was deleted, so carry the message on the fresh one.
    add_session_message(new_session, "Your password has been changed.")
    response = redirect(settings.LOGIN_REDIRECT_URL, status_code=303)
    sessions.set_session_cookie(response, token)
    return response
