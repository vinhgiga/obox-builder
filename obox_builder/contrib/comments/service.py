"""The only writer of comment rows (ADR-0002 D9).

Every operation runs in the request transaction inside a savepoint, validates
mentions against the current permission graph, and maps races to domain errors
that the views translate to HTTP 403/404/409/422/429.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from obox_builder.conf import settings
from obox_builder.contrib.comments.exceptions import (
    CommentConflict,
    CommentNotFound,
    CommentPermissionDenied,
    CommentRateLimited,
    CommentValidationError,
)
from obox_builder.contrib.comments.mentions import parse_mentions, resolve_mentions
from obox_builder.contrib.comments.models import CommentMention, RowComment
from obox_builder.contrib.comments.options import CommentOptions
from obox_builder.contrib.comments.policy import CommentPolicy
from obox_builder.contrib.comments.queries import get_comment
from obox_builder.contrib.comments.schemas import CommentInput, normalize_body, parse_comment_input
from obox_builder.contrib.comments.signals import post_comment, pre_comment
from obox_builder.contrib.comments.targets import CommentTarget
from obox_builder.core.utils.time import ensure_aware, utcnow

RATE_WINDOW_SECONDS = 60


class CommentService:
    """Comment writes for one viewer (``policy``) on one database session."""

    def __init__(self, db: AsyncSession, policy: CommentPolicy) -> None:
        self.db = db
        self.policy = policy

    @property
    def actor_id(self) -> int | None:
        return self.policy.user.id

    @property
    def actor_label(self) -> str:
        user = self.policy.user
        label = getattr(user, "username", "") or str(user)
        return str(label)[:150] or "system"

    # -- add / reply --------------------------------------------------------
    async def add(
        self,
        target: CommentTarget,
        options: CommentOptions,
        data: Mapping[str, Any] | CommentInput,
        *,
        source: str = "admin_ui",
        request_id: str | None = None,
    ) -> RowComment:
        if not options.enabled:
            raise CommentNotFound("Comments are not enabled for this model.")
        if not self.policy.can_add():
            raise CommentPermissionDenied("You cannot comment on this row.")

        payload = (
            data
            if isinstance(data, CommentInput)
            else parse_comment_input(data, max_length=_max_length(options))
        )
        replay = await self._find_replay(payload.idempotency_key, target)
        if replay is not None:
            return replay

        now = utcnow()
        await self.ensure_rate_limit(now=now)
        if payload.parent_id is not None:
            if not options.allow_replies:
                raise CommentValidationError(
                    "Replies are disabled for this model.", field="parent_id"
                )
            parent = await self._live_root(target, payload.parent_id)
            if parent is None:
                raise CommentValidationError(
                    "The comment you are replying to no longer exists.", field="parent_id"
                )
        mentioned = await self._authorise_mentions(payload.body)

        comment = RowComment(
            model_label=target.model_label,
            object_pk=target.object_pk,
            object_repr=target.object_repr[:255],
            parent_id=payload.parent_id,
            body=payload.body,
            body_format="text",
            author_id=self.actor_id,
            author_label=self.actor_label,
            source=source,
            request_id=request_id,
            idempotency_key=payload.idempotency_key,
            created_at=now,
        )
        try:
            async with self.db.begin_nested():
                await pre_comment.send_async(
                    sender=RowComment,
                    action="add",
                    target=target,
                    comment=comment,
                    actor=self.policy.user,
                )
                self.db.add(comment)
                await self.db.flush()
                await self._replace_mentions(comment.id, mentioned)
                await post_comment.send_async(
                    sender=RowComment,
                    action="add",
                    target=target,
                    comment=comment,
                    actor=self.policy.user,
                )
        except IntegrityError as exc:
            replay = await self._find_replay(payload.idempotency_key, target)
            if replay is not None:
                return replay
            raise CommentConflict("The comment could not be saved; please try again.") from exc
        return comment

    # -- edit ---------------------------------------------------------------
    async def edit(
        self,
        target: CommentTarget,
        options: CommentOptions,
        comment_id: int,
        data: Mapping[str, Any],
    ) -> RowComment:
        comment = await self._require(target, comment_id)
        if not self.policy.can_edit(comment, edit_window_seconds=_edit_window(options)):
            raise CommentPermissionDenied("You can no longer edit this comment.")
        body = normalize_body(data.get("body"), max_length=_max_length(options))
        if body == comment.body:
            return comment
        mentioned = await self._authorise_mentions(body)
        try:
            async with self.db.begin_nested():
                await pre_comment.send_async(
                    sender=RowComment,
                    action="edit",
                    target=target,
                    comment=comment,
                    actor=self.policy.user,
                )
                comment.body = body
                comment.edited_at = utcnow()
                await self._replace_mentions(comment.id, mentioned)
                await self.db.flush()
                await post_comment.send_async(
                    sender=RowComment,
                    action="edit",
                    target=target,
                    comment=comment,
                    actor=self.policy.user,
                )
        except StaleDataError as exc:
            raise CommentConflict(
                "This comment was changed by someone else; reload and try again."
            ) from exc
        return comment

    # -- delete -------------------------------------------------------------
    async def delete(self, target: CommentTarget, comment_id: int) -> RowComment:
        comment = await self._require(target, comment_id)
        if comment.deleted_at is not None:
            return comment
        if not self.policy.can_delete(comment):
            raise CommentPermissionDenied("You cannot delete this comment.")
        try:
            async with self.db.begin_nested():
                await pre_comment.send_async(
                    sender=RowComment,
                    action="delete",
                    target=target,
                    comment=comment,
                    actor=self.policy.user,
                )
                comment.deleted_at = utcnow()
                comment.deleted_by_id = self.actor_id
                comment.deleted_by_label = self.actor_label
                await self.db.flush()
                await post_comment.send_async(
                    sender=RowComment,
                    action="delete",
                    target=target,
                    comment=comment,
                    actor=self.policy.user,
                )
        except StaleDataError as exc:
            raise CommentConflict(
                "This comment was changed by someone else; reload and try again."
            ) from exc
        return comment

    # -- resolve / reopen ---------------------------------------------------
    async def set_resolved(
        self, target: CommentTarget, comment_id: int, *, resolved: bool
    ) -> RowComment:
        comment = await self._require(target, comment_id)
        if comment.parent_id is not None:
            raise CommentValidationError("Only root comments can be resolved.", field="parent_id")
        if not self.policy.can_resolve(comment):
            raise CommentPermissionDenied("You cannot resolve this thread.")
        if (comment.resolved_at is not None) == resolved:
            return comment
        try:
            async with self.db.begin_nested():
                await pre_comment.send_async(
                    sender=RowComment,
                    action="resolve" if resolved else "reopen",
                    target=target,
                    comment=comment,
                    actor=self.policy.user,
                )
                if resolved:
                    comment.resolved_at = utcnow()
                    comment.resolved_by_id = self.actor_id
                    comment.resolved_by_label = self.actor_label
                else:
                    comment.resolved_at = None
                    comment.resolved_by_id = None
                    comment.resolved_by_label = None
                await self.db.flush()
                await post_comment.send_async(
                    sender=RowComment,
                    action="resolve" if resolved else "reopen",
                    target=target,
                    comment=comment,
                    actor=self.policy.user,
                )
        except StaleDataError as exc:
            raise CommentConflict(
                "This thread was changed by someone else; reload and try again."
            ) from exc
        return comment

    # -- rate limiting ------------------------------------------------------
    async def ensure_rate_limit(self, *, now: datetime | None = None) -> None:
        """Raise ``CommentRateLimited`` when the author exceeded the window.

        Also used by the mention autocomplete so both paths share one limit.
        """
        limit = int(settings.COMMENTS_RATE_LIMIT_PER_MINUTE)
        if limit <= 0 or self.actor_id is None:
            return
        created_at = await self.db.scalar(
            sa.select(RowComment.created_at)
            .where(RowComment.author_id == self.actor_id)
            .order_by(RowComment.id.desc())
            .offset(limit - 1)
            .limit(1)
        )
        if created_at is None:
            return
        current = now or utcnow()
        elapsed = (current - ensure_aware(created_at)).total_seconds()
        if elapsed < RATE_WINDOW_SECONDS:
            retry_after = max(1, min(RATE_WINDOW_SECONDS, math.ceil(RATE_WINDOW_SECONDS - elapsed)))
            raise CommentRateLimited(retry_after)

    # -- internals ----------------------------------------------------------
    async def _find_replay(
        self, idempotency_key: str | None, target: CommentTarget
    ) -> RowComment | None:
        if not idempotency_key or self.actor_id is None:
            return None
        existing = await self.db.scalar(
            sa.select(RowComment).where(
                RowComment.author_id == self.actor_id,
                RowComment.idempotency_key == idempotency_key,
            )
        )
        if existing is None:
            return None
        if existing.model_label != target.model_label or existing.object_pk != target.object_pk:
            raise CommentConflict("This form was already used for another row.")
        return existing

    async def _live_root(self, target: CommentTarget, parent_id: int) -> RowComment | None:
        return await self.db.scalar(
            sa.select(RowComment).where(
                RowComment.id == parent_id,
                RowComment.model_label == target.model_label,
                RowComment.object_pk == target.object_pk,
                RowComment.parent_id.is_(None),
                RowComment.deleted_at.is_(None),
            )
        )

    async def _require(self, target: CommentTarget, comment_id: int) -> RowComment:
        comment = await get_comment(self.db, target, comment_id)
        if comment is None:
            raise CommentNotFound("Comment not found on this row.")
        return comment

    async def _authorise_mentions(self, body: str) -> dict[int, Any]:
        user_ids = parse_mentions(body)
        if len(user_ids) > int(settings.COMMENTS_MAX_MENTIONS):
            raise CommentValidationError(
                f"A comment may mention at most {settings.COMMENTS_MAX_MENTIONS} users.",
                field="body",
            )
        return await resolve_mentions(
            self.db,
            user_ids,
            model_view_permission=self.policy.view_permission,
        )

    async def _replace_mentions(self, comment_id: int, users: dict[int, Any]) -> None:
        await self.db.execute(
            sa.delete(CommentMention).where(CommentMention.comment_id == comment_id)
        )
        if users:
            await self.db.execute(
                sa.insert(CommentMention),
                [{"comment_id": comment_id, "user_id": user_id} for user_id in users],
            )


def _max_length(options: CommentOptions) -> int:
    return int(options.max_length or settings.COMMENTS_MAX_LENGTH)


def _edit_window(options: CommentOptions) -> int | None:
    if options.edit_window_seconds is not None:
        return int(options.edit_window_seconds)
    return settings.COMMENTS_EDIT_WINDOW_SECONDS


__all__ = ["CommentService"]
