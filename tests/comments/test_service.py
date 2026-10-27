from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from obox_builder.contrib.auth.models import Permission, Role, User
from obox_builder.contrib.auth.permissions import sync_permissions
from obox_builder.contrib.comments import (
    COMMENTS_ADD_PERMISSION,
    COMMENTS_MODERATE_PERMISSION,
    COMMENTS_VIEW_PERMISSION,
)
from obox_builder.contrib.comments.exceptions import (
    CommentConflict,
    CommentNotFound,
    CommentPermissionDenied,
    CommentRateLimited,
    CommentValidationError,
)
from obox_builder.contrib.comments.models import CommentMention, RowComment
from obox_builder.contrib.comments.options import CommentOptions, parse_comments
from obox_builder.contrib.comments.policy import CommentPolicy
from obox_builder.contrib.comments.service import CommentService
from obox_builder.contrib.comments.targets import CommentTarget
from sqlalchemy.ext.asyncio import AsyncSession

VIEW = "commentsdemo.view_ticket"
CHANGE = "commentsdemo.change_ticket"
ALL = frozenset({VIEW, CHANGE, COMMENTS_VIEW_PERMISSION, COMMENTS_ADD_PERMISSION})
TARGET = CommentTarget("commentsdemo.ticket", "1", "Ticket #1")


@dataclass
class FakeUser:
    id: int | None
    permissions: frozenset[str] = ALL
    username: str = "ann"

    def has_perm(self, perm: str) -> bool:
        return perm in self.permissions


def service(db: AsyncSession, user_id: int, permissions: frozenset[str] = ALL) -> CommentService:
    return CommentService(db, CommentPolicy(FakeUser(user_id, permissions), VIEW, CHANGE))


async def _author(db: AsyncSession, username: str = "ann") -> User:
    user = User(username=username, email=f"{username}@example.com")
    db.add(user)
    await db.flush()
    return user


async def _grant(db: AsyncSession, user: User, *names: str) -> None:
    await sync_permissions(db)
    role = Role(name=f"{user.username}-role")
    db.add(role)
    await db.flush()
    await db.refresh(role, ["permissions"])
    for name in names:
        app_label, _, codename = name.partition(".")
        permission = await db.scalar(
            sa.select(Permission).where(
                Permission.app_label == app_label, Permission.codename == codename
            )
        )
        assert permission is not None
        role.permissions.append(permission)
    await db.flush()
    await db.refresh(user, ["roles"])
    user.roles.append(role)
    await db.flush()


def options(**values: object) -> CommentOptions:
    return parse_comments({"allow_replies": True, **values})


async def test_add_stores_a_trimmed_comment(db: AsyncSession, comments_env: None) -> None:
    ann = await _author(db)
    comment = await service(db, ann.id).add(TARGET, options(), {"body": "  hello  "})
    assert comment.body == "hello"
    assert comment.body_format == "text"
    assert comment.author_id == ann.id
    assert comment.author_label == "ann"
    assert comment.object_repr == "Ticket #1"
    assert comment.source == "admin_ui"
    assert comment.parent_id is None


async def test_add_rejects_empty_and_oversized_bodies(db: AsyncSession, comments_env: None) -> None:
    ann = await _author(db)
    repo = service(db, ann.id)
    with pytest.raises(CommentValidationError):
        await repo.add(TARGET, options(max_length=5), {"body": "   "})
    with pytest.raises(CommentValidationError):
        await repo.add(TARGET, options(max_length=5), {"body": "too long"})


async def test_add_requires_permission_and_enabled_model(
    db: AsyncSession, comments_env: None
) -> None:
    ann = await _author(db)
    with pytest.raises(CommentPermissionDenied):
        await service(db, ann.id, frozenset({VIEW})).add(TARGET, options(), {"body": "x"})
    with pytest.raises(CommentNotFound):
        await service(db, ann.id).add(TARGET, CommentOptions(enabled=False), {"body": "x"})


async def test_idempotency_replays_and_rejects_other_targets(
    db: AsyncSession, comments_env: None
) -> None:
    ann = await _author(db)
    repo = service(db, ann.id)
    first = await repo.add(TARGET, options(), {"body": "hello", "idempotency_key": "key-1"})
    again = await repo.add(TARGET, options(), {"body": "different", "idempotency_key": "key-1"})
    assert again.id == first.id
    assert again.body == "hello"
    with pytest.raises(CommentConflict):
        await repo.add(
            CommentTarget("commentsdemo.ticket", "2", "Ticket #2"),
            options(),
            {"body": "hello", "idempotency_key": "key-1"},
        )


async def test_replies_must_target_a_live_root(db: AsyncSession, comments_env: None) -> None:
    ann = await _author(db)
    repo = service(db, ann.id)
    root = await repo.add(TARGET, options(), {"body": "root"})
    reply = await repo.add(TARGET, options(), {"body": "reply", "parent_id": root.id})
    assert reply.parent_id == root.id
    with pytest.raises(CommentValidationError):
        await repo.add(TARGET, options(), {"body": "x", "parent_id": 999})
    await repo.delete(TARGET, root.id)
    with pytest.raises(CommentValidationError):
        await repo.add(TARGET, options(), {"body": "x", "parent_id": root.id})


async def test_replies_can_be_disabled(db: AsyncSession, comments_env: None) -> None:
    ann = await _author(db)
    repo = service(db, ann.id)
    root = await repo.add(TARGET, options(), {"body": "root"})
    with pytest.raises(CommentValidationError):
        await repo.add(TARGET, options(allow_replies=False), {"body": "x", "parent_id": root.id})


async def test_edit_updates_body_and_timestamp(db: AsyncSession, comments_env: None) -> None:
    ann = await _author(db)
    repo = service(db, ann.id)
    comment = await repo.add(TARGET, options(), {"body": "before"})
    edited = await repo.edit(TARGET, options(), comment.id, {"body": "after"})
    assert edited.body == "after"
    assert edited.edited_at is not None
    assert edited.version == 2


async def test_edit_window_and_author_only(db: AsyncSession, comments_env: None) -> None:
    ann = await _author(db)
    bob = await _author(db, "bob")
    repo = service(db, ann.id)
    comment = await repo.add(TARGET, options(), {"body": "before"})
    with pytest.raises(CommentPermissionDenied):
        await service(db, bob.id).edit(TARGET, options(), comment.id, {"body": "nope"})
    comment.created_at = datetime.now(timezone.utc) - timedelta(seconds=3600)
    await db.flush()
    with pytest.raises(CommentPermissionDenied):
        await repo.edit(TARGET, options(edit_window_seconds=60), comment.id, {"body": "late"})
    edited = await repo.edit(TARGET, options(edit_window_seconds=None), comment.id, {"body": "ok"})
    assert edited.body == "ok"


async def test_delete_is_a_tombstone_visible_to_moderators(
    db: AsyncSession, comments_env: None
) -> None:
    ann = await _author(db)
    bob = await _author(db, "bob")
    repo = service(db, ann.id)
    comment = await repo.add(TARGET, options(), {"body": "secret"})
    with pytest.raises(CommentPermissionDenied):
        await service(db, bob.id, frozenset({VIEW})).delete(TARGET, comment.id)
    deleted = await repo.delete(TARGET, comment.id)
    assert deleted.deleted_at is not None
    assert deleted.body == "secret"
    assert deleted.deleted_by_label == "ann"
    second = await repo.add(TARGET, options(), {"body": "moderate me"})
    moderate = service(db, bob.id, frozenset({VIEW, COMMENTS_MODERATE_PERMISSION}))
    assert (await moderate.delete(TARGET, second.id)).deleted_at is not None


async def test_resolve_and_reopen_roots_only(db: AsyncSession, comments_env: None) -> None:
    ann = await _author(db)
    repo = service(db, ann.id)
    root = await repo.add(TARGET, options(), {"body": "root"})
    reply = await repo.add(TARGET, options(), {"body": "reply", "parent_id": root.id})
    resolved = await repo.set_resolved(TARGET, root.id, resolved=True)
    assert resolved.resolved_at is not None
    assert resolved.resolved_by_label == "ann"
    assert (await repo.set_resolved(TARGET, root.id, resolved=True)).version == resolved.version
    reopened = await repo.set_resolved(TARGET, root.id, resolved=False)
    assert reopened.resolved_at is None
    with pytest.raises(CommentValidationError):
        await repo.set_resolved(TARGET, reply.id, resolved=True)


async def test_mentions_are_authorised_and_mirrored(db: AsyncSession, comments_env: None) -> None:
    ann = await _author(db)
    bob = await _author(db, "bob")
    await _grant(db, bob, VIEW, COMMENTS_VIEW_PERMISSION)
    await _grant(db, ann, VIEW, COMMENTS_VIEW_PERMISSION, COMMENTS_ADD_PERMISSION)
    repo = service(db, ann.id)
    comment = await repo.add(TARGET, options(), {"body": f"hi @[u:{bob.id}] and @[u:{ann.id}]"})
    mentions = (
        await db.scalars(
            sa.select(CommentMention.user_id).where(CommentMention.comment_id == comment.id)
        )
    ).all()
    assert set(mentions) == {bob.id, ann.id}


async def test_mention_without_both_view_permissions_is_rejected(
    db: AsyncSession, comments_env: None
) -> None:
    ann = await _author(db)
    bob = await _author(db, "bob")
    await _grant(db, bob, VIEW)
    with pytest.raises(CommentValidationError):
        await service(db, ann.id).add(TARGET, options(), {"body": f"hi @[u:{bob.id}]"})
    with pytest.raises(CommentValidationError):
        await service(db, ann.id).add(TARGET, options(), {"body": "hi @[u:9999]"})


async def test_edit_rewrites_mentions(db: AsyncSession, comments_env: None) -> None:
    ann = await _author(db)
    bob = await _author(db, "bob")
    await _grant(db, bob, VIEW, COMMENTS_VIEW_PERMISSION)
    await _grant(db, ann, VIEW, COMMENTS_ADD_PERMISSION)
    repo = service(db, ann.id)
    comment = await repo.add(TARGET, options(), {"body": f"hi @[u:{bob.id}]"})
    await repo.edit(TARGET, options(), comment.id, {"body": "no mentions"})
    count = await db.scalar(
        sa.select(sa.func.count())
        .select_from(CommentMention)
        .where(CommentMention.comment_id == comment.id)
    )
    assert count == 0


async def test_rate_limit_counts_recent_comments(
    db: AsyncSession, comments_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from obox_builder.conf import settings

    ann = await _author(db)
    repo = service(db, ann.id)
    monkeypatch.setattr(settings._wrapped, "COMMENTS_RATE_LIMIT_PER_MINUTE", 2)
    await repo.add(TARGET, options(), {"body": "one"})
    await repo.add(TARGET, options(), {"body": "two"})
    with pytest.raises(CommentRateLimited) as caught:
        await repo.add(TARGET, options(), {"body": "three"})
    assert caught.value.retry_after >= 1


async def test_stale_write_is_a_conflict(db: AsyncSession, comments_env: None) -> None:
    ann = await _author(db)
    repo = service(db, ann.id)
    comment = await repo.add(TARGET, options(), {"body": "before"})
    await db.execute(
        sa.update(RowComment)
        .where(RowComment.id == comment.id)
        .values(version=comment.version + 1)
        .execution_options(synchronize_session=False)
    )
    with pytest.raises(CommentConflict):
        await repo.edit(TARGET, options(), comment.id, {"body": "after"})


async def test_comment_must_belong_to_the_target(db: AsyncSession, comments_env: None) -> None:
    ann = await _author(db)
    repo = service(db, ann.id)
    comment = await repo.add(TARGET, options(), {"body": "hello"})
    other = service(db, ann.id)
    with pytest.raises(CommentNotFound):
        await other.edit(
            CommentTarget("commentsdemo.ticket", "2", "Ticket #2"),
            options(),
            comment.id,
            {"body": "x"},
        )
