"""Row-scoped comments using the shared policy, queries and writer."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from obox_builder.contrib.admin.api.auth import parse_body
from obox_builder.contrib.admin.api.deps import DB, USER, json_body
from obox_builder.contrib.admin.api.etag import check_if_match, etag
from obox_builder.contrib.admin.api.meta import model_context
from obox_builder.contrib.admin.api.problems import Problem
from obox_builder.contrib.admin.api.records import body_schema, get_record, validate_keys
from obox_builder.contrib.admin.context import AdminContext
from obox_builder.contrib.auth.tokens import scope_allows
from obox_builder.contrib.comments.exceptions import (
    CommentConflict,
    CommentNotFound,
    CommentPermissionDenied,
    CommentRateLimited,
    CommentValidationError,
)
from obox_builder.contrib.comments.models import RowComment
from obox_builder.contrib.comments.policy import CommentPolicy
from obox_builder.contrib.comments.queries import get_comment, thread_page
from obox_builder.contrib.comments.registry import get_options
from obox_builder.contrib.comments.service import CommentService
from obox_builder.contrib.comments.targets import CommentTarget
from obox_builder.core.utils.time import ensure_aware


class CommentEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    body: str = Field(min_length=1, max_length=20000)


class CommentCreate(CommentEdit):
    parent_id: str | None = None
    idempotency_key: str | None = Field(default=None, max_length=64)


class CommentResolve(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    resolved: bool


@dataclass(frozen=True)
class ApiCommentPolicy(CommentPolicy):
    ctx: AdminContext

    def scope(self, action: str) -> bool:
        assert self.ctx.admin is not None  # noqa: S101
        scopes = self.ctx.extra.get("api_scopes")
        return scopes is None or scope_allows(scopes, self.ctx.admin.model_label, action)

    def can_view(self) -> bool:
        assert self.ctx.admin is not None  # noqa: S101
        return super().can_view() and self.ctx.admin.has_comment_view_permission(
            self.ctx, self.ctx.obj
        )

    def can_add(self) -> bool:
        assert self.ctx.admin is not None  # noqa: S101
        return (
            super().can_add()
            and self.scope("write")
            and self.ctx.admin.has_comment_add_permission(self.ctx, self.ctx.obj)
        )

    def can_moderate(self) -> bool:
        assert self.ctx.admin is not None  # noqa: S101
        return super().can_moderate() and self.ctx.admin.has_comment_moderate_permission(
            self.ctx, self.ctx.obj
        )

    def can_delete(self, comment: RowComment) -> bool:
        return self.scope("delete") and super().can_delete(comment)


def comment_etag(row: RowComment) -> str:
    return etag(
        {
            "id": str(row.id),
            "body": row.body,
            "deleted": ensure_aware(row.deleted_at).astimezone(UTC).isoformat()
            if row.deleted_at
            else None,
            "resolved": ensure_aware(row.resolved_at).astimezone(UTC).isoformat()
            if row.resolved_at
            else None,
        },
        row.version,
    )


async def comment_context(
    request: Request, user: Any, db: Any, label: str, pk: str
) -> tuple[Any, ...]:
    ctx = model_context(request, user, db, label)
    admin = ctx.admin
    assert admin is not None  # noqa: S101
    if not admin.comments_available():
        raise Problem(404, "not_found", "Comments are unavailable.")
    obj = await get_record(ctx, pk, lock=request.method != "GET")
    policy = ApiCommentPolicy(
        user, admin.permission_name("view"), admin.permission_name("change"), ctx
    )
    if not policy.can_view():
        raise Problem(403, "permission_denied", "You cannot view comments.")
    target = CommentTarget(label, str(admin.get_pk(obj)), str(obj)[:255])
    return ctx, target, policy, get_options(admin.model)


def register_comments(router: APIRouter) -> None:
    base = "/models/{label}/records/{pk}/comments"

    @router.get(base)
    async def listing(
        request: Request,
        label: str,
        pk: str,
        user: USER,
        db: DB,
        before: int | None = Query(None, ge=1),
        limit: int = Query(20, ge=1, le=100),
    ) -> Any:
        _ctx, target, policy, options = await comment_context(request, user, db, label, pk)
        page = await thread_page(db, target, policy, options, before=before, limit=limit)
        comments = [item for thread in page.threads for item in (thread.root, *thread.replies)]
        rows = list(
            await db.scalars(
                sa.select(RowComment).where(RowComment.id.in_([c.id for c in comments]))
            )
        )
        tags = {row.id: comment_etag(row) for row in rows}

        def encode(comment: Any) -> dict[str, Any]:
            result = asdict(comment)
            for name in ("id", "parent_id", "author_id"):
                result[name] = str(result[name]) if result[name] is not None else None
            result["body_html"] = str(result.pop("body"))
            result["etag"] = tags[comment.id]
            for mention in result["mentions"]:
                mention["user_id"] = str(mention["user_id"])
            return result

        return {
            "threads": [
                {"root": encode(t.root), "replies": [encode(c) for c in t.replies]}
                for t in page.threads
            ],
            "count": page.count,
            "before": str(page.older) if page.older is not None else None,
        }

    async def write(
        request: Request,
        label: str,
        pk: str,
        user: Any,
        db: Any,
        comment_id: int | None = None,
        *,
        resolve: bool = False,
    ) -> Response:
        _ctx, target, policy, options = await comment_context(request, user, db, label, pk)
        service = CommentService(db, policy)
        try:
            if comment_id is not None:
                row = await get_comment(db, target, comment_id)
                if row is None:
                    raise CommentNotFound("Comment is unavailable.")
                await db.refresh(row, with_for_update=True)
                check_if_match(comment_etag(row), request.headers.get("if-match"))
            if request.method == "DELETE":
                if not policy.scope("delete"):
                    raise CommentPermissionDenied("Token cannot delete comments.")
                assert comment_id is not None  # noqa: S101
                await service.delete(target, comment_id)
                return Response(status_code=204)
            schema = (
                CommentCreate if comment_id is None else CommentResolve if resolve else CommentEdit
            )
            data = parse_body(schema, await json_body(request)).model_dump(exclude_unset=True)
            if comment_id is None:
                validate_keys(data, {"body"}, {"parent_id", "idempotency_key"})
                row = await service.add(
                    target,
                    options,
                    data,
                    source="api",
                    request_id=getattr(request.state, "request_id", None),
                )
            elif resolve:
                validate_keys(data, {"resolved"})
                if not isinstance(data["resolved"], bool) or not policy.scope("write"):
                    raise Problem(
                        422 if policy.scope("write") else 403,
                        "invalid_request",
                        "Invalid resolution request.",
                    )
                row = await service.set_resolved(target, comment_id, resolved=data["resolved"])
            else:
                validate_keys(data, {"body"})
                row = await service.edit(target, options, comment_id, data)
        except CommentValidationError as exc:
            raise Problem(
                422, "validation_error", exc.message, errors={exc.field: [exc.message]}
            ) from exc
        except CommentNotFound as exc:
            raise Problem(404, "not_found", str(exc)) from exc
        except CommentPermissionDenied as exc:
            raise Problem(403, "permission_denied", str(exc)) from exc
        except CommentConflict as exc:
            raise Problem(409, "conflict", str(exc)) from exc
        except CommentRateLimited as exc:
            raise Problem(429, "rate_limited", str(exc), retry_after=exc.retry_after) from exc
        return JSONResponse(
            jsonable_encoder({"id": str(row.id), "etag": comment_etag(row)}),
            status_code=201 if comment_id is None else 200,
            headers={"ETag": comment_etag(row)},
        )

    @router.post(base, status_code=201, openapi_extra=body_schema(CommentCreate))
    async def add(request: Request, label: str, pk: str, user: USER, db: DB) -> Response:
        return await write(request, label, pk, user, db)

    @router.patch(base + "/{comment_id}", openapi_extra=body_schema(CommentEdit))
    async def edit(
        request: Request, label: str, pk: str, comment_id: int, user: USER, db: DB
    ) -> Response:
        return await write(request, label, pk, user, db, comment_id)

    @router.delete(base + "/{comment_id}", status_code=204)
    async def delete(
        request: Request, label: str, pk: str, comment_id: int, user: USER, db: DB
    ) -> Response:
        return await write(request, label, pk, user, db, comment_id)

    @router.post(base + "/{comment_id}/resolve", openapi_extra=body_schema(CommentResolve))
    async def resolved(
        request: Request, label: str, pk: str, comment_id: int, user: USER, db: DB
    ) -> Response:
        return await write(request, label, pk, user, db, comment_id, resolve=True)
