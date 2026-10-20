"""Read models for the comment UI.

Queries return frozen dataclasses already shaped for the templates (escaped
bodies, per-comment capabilities), so templates never touch ORM rows. The list
badge is exactly one grouped query per page.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from markupsafe import Markup
from sqlalchemy.ext.asyncio import AsyncSession

from obox_builder.contrib.auth.models import User
from obox_builder.contrib.comments.models import CommentMention, RowComment
from obox_builder.contrib.comments.options import CommentOptions
from obox_builder.contrib.comments.policy import CommentPolicy
from obox_builder.contrib.comments.render import render_body
from obox_builder.contrib.comments.targets import CommentTarget
from obox_builder.core.utils.time import utcnow

DEFAULT_PAGE_SIZE = 20


@dataclass(frozen=True)
class MentionView:
    user_id: int
    label: str


@dataclass(frozen=True)
class CommentView:
    id: int
    parent_id: int | None
    body: Markup
    raw_body: str | None
    author_id: int | None
    author_label: str
    created_at: datetime
    edited_at: datetime | None
    resolved_at: datetime | None
    resolved_by_label: str | None
    deleted: bool
    deleted_by_label: str | None
    can_edit: bool
    can_delete: bool
    can_resolve: bool
    mentions: tuple[MentionView, ...] = ()


@dataclass(frozen=True)
class ThreadView:
    root: CommentView
    replies: tuple[CommentView, ...] = ()


@dataclass(frozen=True)
class ThreadPage:
    threads: tuple[ThreadView, ...]
    count: int
    older: int | None


async def get_comment(
    db: AsyncSession, target: CommentTarget, comment_id: int
) -> RowComment | None:
    """A comment scoped to its target row (never look one up by id alone)."""
    return await db.scalar(
        sa.select(RowComment).where(
            RowComment.id == comment_id,
            RowComment.model_label == target.model_label,
            RowComment.object_pk == target.object_pk,
        )
    )


async def comment_count(db: AsyncSession, target: CommentTarget) -> int:
    total = await db.scalar(
        sa.select(sa.func.count())
        .select_from(RowComment)
        .where(
            RowComment.model_label == target.model_label,
            RowComment.object_pk == target.object_pk,
            RowComment.deleted_at.is_(None),
        )
    )
    return int(total or 0)


async def counts_for_page(
    db: AsyncSession, model_label: str, object_pks: list[str]
) -> dict[str, int]:
    """One grouped query for the list-page badges (ADR-0002 D10)."""
    if not object_pks:
        return {}
    statement = (
        sa.select(RowComment.object_pk, sa.func.count())
        .where(
            RowComment.model_label == model_label,
            RowComment.object_pk.in_(object_pks),
            RowComment.deleted_at.is_(None),
        )
        .group_by(RowComment.object_pk)
    )
    return {str(pk): int(count) for pk, count in (await db.execute(statement)).all()}


async def latest_object_repr(db: AsyncSession, target: CommentTarget) -> str:
    value = await db.scalar(
        sa.select(RowComment.object_repr)
        .where(
            RowComment.model_label == target.model_label,
            RowComment.object_pk == target.object_pk,
        )
        .order_by(RowComment.id.desc())
        .limit(1)
    )
    return str(value or "")


async def thread_page(
    db: AsyncSession,
    target: CommentTarget,
    policy: CommentPolicy,
    options: CommentOptions,
    *,
    before: int | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
) -> ThreadPage:
    """One keyset page of root threads plus their replies (two queries)."""
    statement = sa.select(RowComment).where(
        RowComment.model_label == target.model_label,
        RowComment.object_pk == target.object_pk,
        RowComment.parent_id.is_(None),
    )
    if before is not None:
        statement = statement.where(RowComment.id < before)
    rows = list((await db.scalars(statement.order_by(RowComment.id.desc()).limit(limit + 1))).all())
    roots = rows[:limit]
    older = roots[-1].id if len(rows) > limit else None
    root_ids = [row.id for row in roots]
    replies: list[RowComment] = []
    if root_ids:
        replies = list(
            (
                await db.scalars(
                    sa.select(RowComment)
                    .where(RowComment.parent_id.in_(root_ids))
                    .order_by(RowComment.id.asc())
                )
            ).all()
        )
    names = await _mention_names(db, [*root_ids, *(row.id for row in replies)])
    now = utcnow()
    reply_views: dict[int, list[CommentView]] = {}
    for row in replies:
        reply_views.setdefault(int(row.parent_id or 0), []).append(
            _comment_view(row, policy, options, names, now)
        )
    threads = tuple(
        ThreadView(
            root=_comment_view(row, policy, options, names, now),
            replies=tuple(reply_views.get(row.id, ())),
        )
        for row in roots
    )
    return ThreadPage(
        threads=threads,
        count=await comment_count(db, target),
        older=older,
    )


async def latest_threads(
    db: AsyncSession,
    target: CommentTarget,
    policy: CommentPolicy,
    options: CommentOptions,
    *,
    limit: int = 5,
) -> ThreadPage:
    return await thread_page(db, target, policy, options, limit=limit)


async def _mention_names(db: AsyncSession, comment_ids: list[int]) -> dict[int, dict[int, str]]:
    if not comment_ids:
        return {}
    statement = (
        sa.select(
            CommentMention.comment_id,
            User.id,
            User.username,
            User.full_name,
        )
        .join(User, User.id == CommentMention.user_id)
        .where(CommentMention.comment_id.in_(comment_ids))
    )
    result: dict[int, dict[int, str]] = {}
    for comment_id, user_id, username, full_name in (await db.execute(statement)).all():
        result.setdefault(int(comment_id), {})[int(user_id)] = (
            full_name or username or f"user #{user_id}"
        )[:150]
    return result


def _comment_view(
    row: RowComment,
    policy: CommentPolicy,
    options: CommentOptions,
    names: dict[int, dict[int, str]],
    now: datetime,
) -> CommentView:
    deleted = row.deleted_at is not None
    visible_body = row.body if (not deleted or policy.can_read_deleted()) else None
    can_edit = policy.can_edit(row, edit_window_seconds=options.edit_window_seconds, now=now)
    mention_map = names.get(int(row.id), {})
    mentions = tuple(
        MentionView(user_id=user_id, label=label) for user_id, label in sorted(mention_map.items())
    )
    return CommentView(
        id=int(row.id),
        parent_id=int(row.parent_id) if row.parent_id is not None else None,
        body=render_body(visible_body, mention_map),
        raw_body=row.body if can_edit else None,
        author_id=int(row.author_id) if row.author_id is not None else None,
        author_label=row.author_label,
        created_at=row.created_at,
        edited_at=row.edited_at,
        resolved_at=row.resolved_at,
        resolved_by_label=row.resolved_by_label,
        deleted=deleted,
        deleted_by_label=row.deleted_by_label,
        can_edit=can_edit,
        can_delete=policy.can_delete(row),
        can_resolve=policy.can_resolve(row),
        mentions=mentions,
    )


__all__ = [
    "CommentView",
    "MentionView",
    "ThreadPage",
    "ThreadView",
    "comment_count",
    "counts_for_page",
    "get_comment",
    "latest_object_repr",
    "latest_threads",
    "thread_page",
]
