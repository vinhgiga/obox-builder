"""Admin views for row comments (standalone page, composer and actions)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from obox_builder.conf import settings
from obox_builder.contrib.admin.context import AdminContext
from obox_builder.contrib.admin.options import ModelView
from obox_builder.contrib.admin.rendering import render
from obox_builder.contrib.admin.site import AdminSite
from obox_builder.contrib.admin.views.common import (
    current_site,
    ensure_csrf,
    flash,
    page_context,
)
from obox_builder.contrib.auth.dependencies import permission_required
from obox_builder.contrib.comments import (
    COMMENTS_ADD_PERMISSION,
    COMMENTS_VIEW_PERMISSION,
)
from obox_builder.contrib.comments.exceptions import (
    CommentConflict,
    CommentError,
    CommentNotFound,
    CommentPermissionDenied,
    CommentRateLimited,
    CommentValidationError,
)
from obox_builder.contrib.comments.mentions import mentionable_users
from obox_builder.contrib.comments.options import CommentOptions
from obox_builder.contrib.comments.policy import CommentPolicy
from obox_builder.contrib.comments.queries import latest_object_repr, thread_page
from obox_builder.contrib.comments.registry import get_options
from obox_builder.contrib.comments.service import CommentService
from obox_builder.contrib.comments.targets import CommentTarget
from obox_builder.db.session import get_db
from obox_builder.exceptions import ImproperlyConfigured, PermissionDenied

__all__ = ["register_comment_routes", "mentionable"]

_MENTION_ADD_DEPENDENCY = Depends(permission_required(COMMENTS_ADD_PERMISSION))


def register_comment_routes(router: APIRouter, site: AdminSite, admin: ModelView) -> None:
    """Attach the comment endpoints for one registered ``ModelView``."""
    base = f"/{admin.path_prefix}"
    name = admin.route_prefix
    view_dep = Depends(permission_required(admin.permission_name("view")))
    comment_view_dep = Depends(permission_required(COMMENTS_VIEW_PERMISSION))
    comment_add_dep = Depends(permission_required(COMMENTS_ADD_PERMISSION))

    async def comments_page(
        request: Request,
        pk: str,
        user: Any = view_dep,
        _comment_view: Any = comment_view_dep,
        db: AsyncSession = Depends(get_db),
    ) -> Any:
        ctx = _context(request, user, db, site, admin)
        return await render_page(ctx, pk)

    async def add_comment(
        request: Request,
        pk: str,
        user: Any = view_dep,
        _comment_add: Any = comment_add_dep,
        db: AsyncSession = Depends(get_db),
    ) -> Any:
        ctx = _context(request, user, db, site, admin)
        data = await _form_data(request)
        await ensure_csrf(request, data.get("csrf_token"))
        try:
            _obj, target = await _load_target(ctx, pk, allow_deleted=False)
            comment = await _execute_comment(
                ctx,
                "addComment",
                pk,
                {
                    name: data[name]
                    for name in ("body", "parent_id", "idempotency_key")
                    if name in data
                },
            )
        except CommentValidationError as exc:
            return await render_page(
                ctx,
                pk,
                status_code=422,
                error=exc.message,
                field=exc.field,
                body_value=_raw_body(data),
                reply_parent=_parent_id(data),
            )
        except CommentRateLimited as exc:
            return await render_page(
                ctx,
                pk,
                status_code=429,
                error=str(exc),
                body_value=_raw_body(data),
                reply_parent=_parent_id(data),
                headers={"Retry-After": str(exc.retry_after)},
            )
        except CommentError as exc:
            return _failure(ctx, exc, pk)
        await flash(request, "Comment posted.")
        return RedirectResponse(f"{admin.comments_path(pk)}#comment-{comment.id}", status_code=303)

    async def edit_comment(
        request: Request,
        pk: str,
        comment_id: int,
        user: Any = view_dep,
        _comment_add: Any = comment_add_dep,
        db: AsyncSession = Depends(get_db),
    ) -> Any:
        ctx = _context(request, user, db, site, admin)
        data = await _form_data(request)
        await ensure_csrf(request, data.get("csrf_token"))
        try:
            _obj, target = await _load_target(ctx, pk, allow_deleted=False)
            await _execute_comment(
                ctx,
                "editComment",
                pk,
                {
                    "comment_id": str(comment_id),
                    "version": data.get("version"),
                    "body": data.get("body", ""),
                },
            )
        except CommentValidationError as exc:
            return await render_page(
                ctx,
                pk,
                status_code=422,
                error=exc.message,
                field=exc.field,
                edit_comment_id=comment_id,
                edit_body=_raw_body(data),
            )
        except CommentError as exc:
            return _failure(ctx, exc, pk)
        await flash(request, "Comment updated.")
        return RedirectResponse(f"{admin.comments_path(pk)}#comment-{comment_id}", status_code=303)

    async def delete_comment(
        request: Request,
        pk: str,
        comment_id: int,
        user: Any = view_dep,
        _comment_add: Any = comment_add_dep,
        db: AsyncSession = Depends(get_db),
    ) -> Any:
        ctx = _context(request, user, db, site, admin)
        data = await _form_data(request)
        await ensure_csrf(request, data.get("csrf_token"))
        try:
            _obj, target = await _load_target(ctx, pk, allow_deleted=False)
            operation = (
                "moderateComment"
                if user.has_perm("comments.rowComment.moderate")
                else "deleteComment"
            )
            await _execute_comment(
                ctx, operation, pk, {"comment_id": str(comment_id), "version": data.get("version")}
            )
        except CommentError as exc:
            return _failure(ctx, exc, pk)
        await flash(request, "Comment deleted.")
        return RedirectResponse(admin.comments_path(pk), status_code=303)

    async def resolve_comment(
        request: Request,
        pk: str,
        comment_id: int,
        user: Any = view_dep,
        _comment_add: Any = comment_add_dep,
        db: AsyncSession = Depends(get_db),
    ) -> Any:
        return await _set_resolved(request, pk, comment_id, user, db, site, admin, resolved=True)

    async def reopen_comment(
        request: Request,
        pk: str,
        comment_id: int,
        user: Any = view_dep,
        _comment_add: Any = comment_add_dep,
        db: AsyncSession = Depends(get_db),
    ) -> Any:
        return await _set_resolved(request, pk, comment_id, user, db, site, admin, resolved=False)

    router.add_api_route(
        f"{base}/{{pk}}/comments",
        comments_page,
        methods=["GET"],
        name=f"{name}_comments",
    )
    router.add_api_route(
        f"{base}/{{pk}}/comments",
        add_comment,
        methods=["POST"],
        name=f"{name}_comment_add",
    )
    router.add_api_route(
        f"{base}/{{pk}}/comments/{{comment_id}}/edit",
        edit_comment,
        methods=["POST"],
        name=f"{name}_comment_edit",
    )
    router.add_api_route(
        f"{base}/{{pk}}/comments/{{comment_id}}/delete",
        delete_comment,
        methods=["POST"],
        name=f"{name}_comment_delete",
    )
    router.add_api_route(
        f"{base}/{{pk}}/comments/{{comment_id}}/resolve",
        resolve_comment,
        methods=["POST"],
        name=f"{name}_comment_resolve",
    )
    router.add_api_route(
        f"{base}/{{pk}}/comments/{{comment_id}}/reopen",
        reopen_comment,
        methods=["POST"],
        name=f"{name}_comment_reopen",
    )


async def mentionable(
    request: Request,
    model: str = "",
    q: str = "",
    user: Any = _MENTION_ADD_DEPENDENCY,
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Autocomplete candidates holding both view permissions (two chars min)."""
    site = current_site(request)
    admin = site.get_registry().get(model)
    if admin is None or not admin.comments_available():
        raise HTTPException(status_code=404, detail="Unknown commentable model.")
    ctx = _context(request, user, db, site, admin)
    if not admin.has_comment_add_permission(ctx):
        raise PermissionDenied("You cannot mention users on this row.")
    service = CommentService(db, _policy(admin, user))
    try:
        await service.ensure_rate_limit()
    except CommentRateLimited as exc:
        return JSONResponse(
            {"detail": str(exc)},
            status_code=429,
            headers={"Retry-After": str(exc.retry_after)},
        )
    candidates = await mentionable_users(db, q, model_view_permission=admin.permission_name("view"))
    return JSONResponse(
        {"results": [{"id": candidate.id, "label": candidate.label} for candidate in candidates]}
    )


# -- page rendering ---------------------------------------------------------


async def render_page(
    ctx: AdminContext,
    pk: str,
    *,
    status_code: int = 200,
    error: str = "",
    field: str = "",
    body_value: str = "",
    reply_parent: int | None = None,
    edit_comment_id: int | None = None,
    edit_body: str = "",
    headers: dict[str, str] | None = None,
) -> HTMLResponse:
    admin = _require_admin(ctx)
    request = ctx.request
    try:
        obj, target = await _load_target(ctx, pk, allow_deleted=True)
    except CommentNotFound as exc:
        return _problem(
            request,
            title="Comments not found",
            message=str(exc),
            status_code=404,
            back_url=admin.list_path(),
        )
    options = get_options(admin.model)
    policy = _policy(admin, ctx.user)
    page = await thread_page(ctx.db, target, policy, options, before=_before_cursor(request))
    can_add = obj is not None and policy.can_add() and options.enabled
    context = await page_context(
        request,
        admin=admin,
        heading=f"{admin.name} comments",
        subtitle=target.object_repr or f"#{pk}",
        comments_url=admin.comments_path(pk),
        back_url=admin.change_path(pk) if obj is not None else admin.list_path(),
        count=page.count,
        threads=page.threads,
        older=page.older,
        before=_before_cursor(request),
        can_add=can_add,
        allow_replies=options.allow_replies,
        deleted_target=obj is None,
        error=error,
        error_field=field,
        body_value=body_value,
        reply_parent=reply_parent,
        edit_comment_id=edit_comment_id,
        edit_body=edit_body,
        idempotency_key=uuid.uuid4().hex,
        mention_url="/admin/comments/mentionable",
        mention_model=admin.model_label,
        max_length=_max_length(options),
    )
    response = render(request, "admin/comments.html", context, status_code=status_code)
    if headers:
        response.headers.update(headers)
    return response


async def _set_resolved(
    request: Request,
    pk: str,
    comment_id: int,
    user: Any,
    db: AsyncSession,
    site: AdminSite,
    admin: ModelView,
    *,
    resolved: bool,
) -> Any:
    ctx = _context(request, user, db, site, admin)
    data = await _form_data(request)
    await ensure_csrf(request, data.get("csrf_token"))
    try:
        _obj, target = await _load_target(ctx, pk, allow_deleted=False)
        await _execute_comment(
            ctx,
            "resolveComment",
            pk,
            {"comment_id": str(comment_id), "version": data.get("version"), "resolved": resolved},
        )
    except CommentError as exc:
        return _failure(ctx, exc, pk)
    await flash(request, "Thread resolved." if resolved else "Thread reopened.")
    return RedirectResponse(f"{admin.comments_path(pk)}#comment-{comment_id}", status_code=303)


async def _execute_comment(ctx: AdminContext, operation: str, pk: str, data: dict[str, Any]) -> Any:
    from obox_builder.resources import ResourceValidationError

    try:
        return await _require_admin(ctx).resource.execute(
            operation, ctx.operation_context(), data, target=pk
        )
    except ResourceValidationError as exc:
        field, errors = next(iter(exc.errors.items()))
        raise CommentValidationError(errors[0], field=field) from exc
    except LookupError as exc:
        raise CommentNotFound(str(exc)) from exc


def _failure(ctx: AdminContext, exc: CommentError, pk: str) -> HTMLResponse:
    admin = _require_admin(ctx)
    if isinstance(exc, CommentPermissionDenied):
        status, title = 403, "Permission denied"
    elif isinstance(exc, CommentNotFound):
        status, title = 404, "Comment not found"
    elif isinstance(exc, CommentConflict):
        status, title = 409, "Comment changed"
    elif isinstance(exc, CommentRateLimited):
        status, title = 429, "Too many comments"
    else:
        status, title = 422, "Invalid comment"
    return _problem(
        ctx.request,
        title=title,
        message=str(exc),
        status_code=status,
        back_url=admin.comments_path(pk),
    )


def _problem(
    request: Request,
    *,
    title: str,
    message: str,
    status_code: int,
    back_url: str,
) -> HTMLResponse:
    context = {
        "site_name": "obox admin",
        "title": title,
        "message": message,
        "back_url": back_url,
    }
    return render(request, "admin/errors/problem.html", context, status_code=status_code)


# -- helpers ----------------------------------------------------------------


def _context(
    request: Request,
    user: Any,
    db: AsyncSession,
    site: AdminSite,
    admin: ModelView,
) -> AdminContext:
    return AdminContext(request=request, user=user, db=db, site=site, admin=admin)


def _policy(admin: ModelView, user: Any) -> CommentPolicy:
    return CommentPolicy(
        user=user,
        view_permission=admin.permission_name("view"),
        change_permission=admin.permission_name("change"),
    )


def _require_admin(ctx: AdminContext) -> ModelView:
    if ctx.admin is None:  # pragma: no cover - comment routes always set it
        raise ImproperlyConfigured("AdminContext without a ModelView on a comment route.")
    return ctx.admin


async def _load_target(
    ctx: AdminContext, pk: str, *, allow_deleted: bool
) -> tuple[Any | None, CommentTarget]:
    admin = _require_admin(ctx)
    try:
        value = admin.parse_pk(pk)
    except LookupError as exc:
        raise CommentNotFound(str(exc)) from None
    obj = await ctx.db.get(admin.model, value)
    target = CommentTarget(admin.model_label, str(value))
    if obj is not None:
        try:
            obj = await admin.get_object(ctx, pk)
        except LookupError as exc:
            raise CommentNotFound(str(exc)) from None
        ctx.obj = obj
        admin.check_permission(ctx, "view", obj)
        admin.check_permission(ctx, "comment_view" if allow_deleted else "comment_add", obj)
        return obj, target.with_repr(_object_label(admin, obj))
    if not allow_deleted:
        raise CommentNotFound(f"{admin.name} with id {pk!r} does not exist.")
    admin.check_permission(ctx, "comment_view")
    snapshot = await latest_object_repr(ctx.db, target)
    if not snapshot:
        raise CommentNotFound(f"{admin.name} with id {pk!r} does not exist.")
    return None, target.with_repr(snapshot)


def _object_label(admin: ModelView, obj: Any) -> str:
    text = str(obj)
    if text.startswith("<"):
        return f"#{admin.get_pk(obj)}"
    return text


async def _form_data(request: Request) -> dict[str, Any]:
    form = await request.form()
    return {key: value for key, value in form.multi_items() if isinstance(value, str)}


def _raw_body(data: dict[str, Any]) -> str:
    value = data.get("body")
    return value if isinstance(value, str) else ""


def _parent_id(data: dict[str, Any]) -> int | None:
    try:
        value = int(data.get("parent_id") or 0)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _request_id(request: Request) -> str | None:
    value = getattr(request.state, "request_id", None)
    return str(value)[:64] if value else None


def _before_cursor(request: Request) -> int | None:
    raw = request.query_params.get("before")
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def _max_length(options: CommentOptions) -> int:
    return int(options.max_length or settings.COMMENTS_MAX_LENGTH)
