from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa
from obox_builder.contrib.auth.models import User
from obox_builder.contrib.comments.models import CommentMention, RowComment
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


def now() -> datetime:
    return datetime.now(UTC)


async def _violates(db: AsyncSession, statement: Any) -> None:
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await db.execute(statement)


async def _users(db: AsyncSession) -> tuple[User, User]:
    ann = User(username="ann", email="ann@example.com")
    bob = User(username="bob", email="bob@example.com")
    db.add_all([ann, bob])
    await db.flush()
    return ann, bob


def _comment(**values: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "model_label": "commentsdemo.ticket",
        "object_pk": "42",
        "author_label": "ann",
        "created_at": now(),
        "body": "hello",
    }
    data.update(values)
    return data


async def test_root_and_reply_on_same_target(db: AsyncSession) -> None:
    ann, _bob = await _users(db)
    root = RowComment(**_comment(author_id=ann.id))
    db.add(root)
    await db.flush()
    reply = RowComment(**_comment(author_id=ann.id, parent_id=root.id))
    db.add(reply)
    await db.flush()
    assert reply.id != root.id


async def test_reply_on_another_row_is_rejected(db: AsyncSession) -> None:
    ann, _bob = await _users(db)
    root = RowComment(**_comment(author_id=ann.id))
    db.add(root)
    await db.flush()
    await _violates(db, sa.insert(RowComment).values(**_comment(parent_id=root.id, object_pk="43")))


async def test_resolving_a_reply_is_rejected(db: AsyncSession) -> None:
    ann, _bob = await _users(db)
    root = RowComment(**_comment(author_id=ann.id))
    db.add(root)
    await db.flush()
    reply = RowComment(**_comment(author_id=ann.id, parent_id=root.id))
    db.add(reply)
    await db.flush()
    await _violates(
        db,
        sa.update(RowComment).where(RowComment.id == reply.id).values(resolved_at=now()),
    )


async def test_live_comment_needs_a_body(db: AsyncSession) -> None:
    await _users(db)
    await _violates(db, sa.insert(RowComment).values(**_comment(body=None)))


async def test_body_length_is_capped(db: AsyncSession) -> None:
    await _users(db)
    await _violates(db, sa.insert(RowComment).values(**_comment(body="x" * 20_001)))


async def test_hard_delete_of_a_root_with_replies_is_restricted(db: AsyncSession) -> None:
    ann, _bob = await _users(db)
    root = RowComment(**_comment(author_id=ann.id))
    db.add(root)
    await db.flush()
    db.add(RowComment(**_comment(author_id=ann.id, parent_id=root.id)))
    await db.flush()
    await _violates(db, sa.delete(RowComment).where(RowComment.id == root.id))


async def test_bad_source_is_rejected(db: AsyncSession) -> None:
    await _users(db)
    await _violates(db, sa.insert(RowComment).values(**_comment(source="hax")))


async def test_tombstone_without_body_is_allowed(db: AsyncSession) -> None:
    ann, _bob = await _users(db)
    root = RowComment(**_comment(author_id=ann.id))
    db.add(root)
    await db.flush()
    root.deleted_at = now()
    root.deleted_by_label = "ann"
    root.body = None
    await db.flush()
    assert root.deleted_at is not None


async def test_idempotency_is_unique_per_author(db: AsyncSession) -> None:
    ann, bob = await _users(db)
    db.add(RowComment(**_comment(author_id=ann.id, idempotency_key="k1")))
    await db.flush()
    await _violates(
        db,
        sa.insert(RowComment).values(**_comment(author_id=ann.id, idempotency_key="k1")),
    )
    db.add(RowComment(**_comment(author_id=bob.id, idempotency_key="k1")))
    db.add(RowComment(**_comment(author_id=ann.id)))
    db.add(RowComment(**_comment(author_id=ann.id)))
    await db.flush()


async def test_deleting_the_author_keeps_the_comment(db: AsyncSession) -> None:
    ann, _bob = await _users(db)
    root = RowComment(**_comment(author_id=ann.id))
    db.add(root)
    await db.flush()
    await db.delete(ann)
    await db.flush()
    await db.refresh(root)
    assert root.author_id is None
    assert root.author_label == "ann"


async def test_mentions_cascade_with_the_comment_and_user(db: AsyncSession) -> None:
    ann, bob = await _users(db)
    root = RowComment(**_comment(author_id=ann.id))
    db.add(root)
    await db.flush()
    db.add(CommentMention(comment_id=root.id, user_id=bob.id))
    await db.flush()
    await db.delete(bob)
    await db.flush()
    remaining = await db.scalar(sa.select(sa.func.count()).select_from(CommentMention))
    assert remaining == 0
