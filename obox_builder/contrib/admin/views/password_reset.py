"""Public password reset request and confirmation views."""

from __future__ import annotations

import logging

from fastapi import Depends, Form, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from obox_builder.contrib.admin.rendering import render
from obox_builder.contrib.admin.views.common import page_context
from obox_builder.contrib.auth import csrf, password_reset
from obox_builder.contrib.auth.validators import PasswordValidationError
from obox_builder.db.session import get_db
from obox_builder.http.shortcuts import redirect

logger = logging.getLogger(__name__)


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
    valid = await password_reset.is_valid_reset_token(db, token)
    return render(
        request,
        "admin/auth/password_reset_confirm.html",
        await page_context(request, token=token, valid=valid),
        status_code=200 if valid else 400,
    )


async def password_reset_confirm_submit(
    request: Request,
    token: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
    db: AsyncSession = Depends(get_db),
) -> Response:
    if not csrf.check_origin(request):
        return render(
            request,
            "admin/auth/password_reset_confirm.html",
            await page_context(
                request,
                token=token,
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
                request, token=token, valid=True, errors=["The two password fields do not match."]
            ),
        )
    try:
        reset = await password_reset.reset_password(db, token, new_password)
    except PasswordValidationError as exc:
        return render(
            request,
            "admin/auth/password_reset_confirm.html",
            await page_context(request, token=token, valid=True, errors=exc.errors),
        )
    if not reset:
        return render(
            request,
            "admin/auth/password_reset_confirm.html",
            await page_context(request, token=token, valid=False),
            status_code=400,
        )
    return redirect("/admin/login", status_code=303)
