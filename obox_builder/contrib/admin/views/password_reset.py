"""Public password reset request and confirmation views."""

from __future__ import annotations

import hashlib
import logging
import secrets

from fastapi import Depends, Form, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from obox_builder.conf import settings
from obox_builder.contrib.admin.rendering import render
from obox_builder.contrib.admin.views.common import page_context
from obox_builder.contrib.auth import csrf, password_reset
from obox_builder.contrib.auth.validators import PasswordValidationError
from obox_builder.db.session import get_db
from obox_builder.http.shortcuts import redirect

logger = logging.getLogger(__name__)
RESET_COOKIE = "obox_reset"


def reset_csrf(token: str) -> str:
    return hashlib.sha256(f"reset-csrf:{token}".encode()).hexdigest()


async def password_reset_request_view(request: Request) -> Response:
    return render(
        request,
        "admin/auth/password_reset.html",
        await page_context(request),
    )


async def password_reset_request_submit(
    request: Request,
    email: str = Form(""),
    db: AsyncSession = Depends(get_db),
) -> Response:
    if not csrf.check_origin(request):
        return render(
            request,
            "admin/auth/password_reset.html",
            await page_context(request, error="Cross-origin request rejected."),
            status_code=403,
        )
    try:
        await password_reset.request_password_reset(db, email)
    except Exception:
        logger.exception("Unable to process a password reset request.")
    return render(
        request,
        "admin/auth/password_reset.html",
        await page_context(
            request,
            submitted=True,
            message="If an active account matches that email, a reset link will be sent.",
        ),
    )


async def password_reset_confirm_view(
    request: Request,
    token: str = "",
    db: AsyncSession = Depends(get_db),
) -> Response:
    response: Response
    if token:
        if await password_reset.is_valid_reset_token(db, token):
            response = redirect("/admin/password_reset/confirm", status_code=303)
            response.set_cookie(
                RESET_COOKIE,
                token,
                max_age=settings.PASSWORD_RESET_TIMEOUT,
                httponly=True,
                secure=settings.SESSION_COOKIE_SECURE and not settings.DEBUG,
                samesite="lax",
                path="/admin/password_reset/confirm",
            )
        else:
            response = render(
                request,
                "admin/auth/password_reset_confirm.html",
                await page_context(request, valid=False),
                status_code=400,
            )
    else:
        token = request.cookies.get(RESET_COOKIE, "")
        valid = await password_reset.is_valid_reset_token(db, token)
        response = render(
            request,
            "admin/auth/password_reset_confirm.html",
            await page_context(request, valid=valid, csrf_token=reset_csrf(token)),
            status_code=200 if valid else 400,
        )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


async def password_reset_confirm_submit(
    request: Request,
    csrf_token: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
    db: AsyncSession = Depends(get_db),
) -> Response:
    token = request.cookies.get(RESET_COOKIE, "")
    if (
        not token
        or not secrets.compare_digest(csrf_token.encode(), reset_csrf(token).encode())
        or not csrf.check_origin(request)
    ):
        return render(
            request,
            "admin/auth/password_reset_confirm.html",
            await page_context(
                request,
                csrf_token=reset_csrf(token),
                valid=True,
                errors=["Cross-origin request rejected."],
            ),
            status_code=403,
        )
    if new_password != confirm_password:
        return render(
            request,
            "admin/auth/password_reset_confirm.html",
            await page_context(
                request,
                csrf_token=reset_csrf(token),
                valid=True,
                errors=["The two password fields do not match."],
            ),
        )
    try:
        reset = await password_reset.reset_password(db, token, new_password)
    except PasswordValidationError as exc:
        return render(
            request,
            "admin/auth/password_reset_confirm.html",
            await page_context(
                request, csrf_token=reset_csrf(token), valid=True, errors=exc.errors
            ),
        )
    if not reset:
        return render(
            request,
            "admin/auth/password_reset_confirm.html",
            await page_context(request, valid=False),
            status_code=400,
        )
    response = redirect("/admin/login", status_code=303)
    response.delete_cookie(RESET_COOKIE, path="/admin/password_reset/confirm")
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response
