from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from obox_builder.contrib.comments import (
    COMMENTS_ADD_PERMISSION,
    COMMENTS_MODERATE_PERMISSION,
    COMMENTS_VIEW_PERMISSION,
)
from obox_builder.contrib.comments.models import RowComment
from obox_builder.contrib.comments.policy import CommentPolicy

VIEW = "commentsdemo.view_ticket"
CHANGE = "commentsdemo.change_ticket"


@dataclass
class FakeUser:
    id: int | None
    permissions: frozenset[str]

    def has_perm(self, perm: str) -> bool:
        return perm in self.permissions


def user(*permissions: str, user_id: int = 1) -> FakeUser:
    return FakeUser(user_id, frozenset(permissions))


def policy(viewer: FakeUser) -> CommentPolicy:
    return CommentPolicy(user=viewer, view_permission=VIEW, change_permission=CHANGE)


def comment(*, author_id: int = 1, parent_id: int | None = None, **values: object) -> RowComment:
    data = {
        "model_label": "commentsdemo.ticket",
        "object_pk": "1",
        "author_label": "ann",
        "created_at": datetime.now(timezone.utc),
        "body": "hello",
        "author_id": author_id,
    }
    data.update(values)
    row = RowComment(**data)
    row.parent_id = parent_id
    return row


def test_capability_gates_require_the_model_view_permission() -> None:
    reader = policy(user(COMMENTS_VIEW_PERMISSION))
    assert not reader.can_view()
    assert reader.can_view() is False
    viewer = policy(user(VIEW, COMMENTS_VIEW_PERMISSION))
    assert viewer.can_view()
    assert not viewer.can_add()
    adder = policy(user(VIEW, COMMENTS_ADD_PERMISSION))
    assert adder.can_add()
    assert not adder.can_moderate()
    moderator = policy(user(VIEW, COMMENTS_MODERATE_PERMISSION))
    assert moderator.can_moderate()
    assert moderator.can_read_deleted()


def test_edit_requires_author_add_permission_and_live_comment() -> None:
    author = policy(user(VIEW, COMMENTS_ADD_PERMISSION))
    assert author.can_edit(comment())
    assert not author.can_edit(comment(author_id=2))
    assert not author.can_edit(comment(deleted_at=datetime.now(timezone.utc)))
    assert not policy(user(VIEW)).can_edit(comment())


def test_edit_window_is_enforced() -> None:
    author = policy(user(VIEW, COMMENTS_ADD_PERMISSION))
    old = comment(created_at=datetime.now(timezone.utc) - timedelta(seconds=120))
    assert not author.can_edit(old, edit_window_seconds=60)
    assert author.can_edit(old, edit_window_seconds=300)
    assert author.can_edit(old, edit_window_seconds=None)


def test_delete_author_or_moderator_only() -> None:
    author = policy(user(VIEW, COMMENTS_ADD_PERMISSION))
    assert author.can_delete(comment())
    assert not author.can_delete(comment(author_id=2))
    moderator = policy(user(VIEW, COMMENTS_MODERATE_PERMISSION))
    assert moderator.can_delete(comment(author_id=2))
    assert not moderator.can_delete(comment(deleted_at=datetime.now(timezone.utc)))
    assert not policy(user(VIEW)).can_delete(comment())


def test_resolve_author_model_changer_or_moderator_on_live_roots() -> None:
    author = policy(user(VIEW, COMMENTS_ADD_PERMISSION))
    assert author.can_resolve(comment())
    assert not author.can_resolve(comment(author_id=2))
    assert not author.can_resolve(comment(parent_id=1))
    changer = policy(user(VIEW, CHANGE))
    assert not changer.can_resolve(comment())
    changer_with_comments = policy(user(VIEW, CHANGE, COMMENTS_ADD_PERMISSION))
    assert changer_with_comments.can_resolve(comment(author_id=2))
    moderator = policy(user(VIEW, COMMENTS_MODERATE_PERMISSION))
    assert not moderator.can_resolve(comment())
    moderator_with_add = policy(user(VIEW, COMMENTS_MODERATE_PERMISSION, COMMENTS_ADD_PERMISSION))
    assert moderator_with_add.can_resolve(comment(author_id=2))
    assert not author.can_resolve(comment(deleted_at=datetime.now(timezone.utc)))


def test_anonymous_viewer_has_no_capabilities() -> None:
    anonymous = policy(FakeUser(None, frozenset()))
    assert not anonymous.can_view()
    assert not anonymous.can_add()
    assert not anonymous.can_moderate()
    assert not anonymous.can_edit(comment())
    assert not anonymous.can_delete(comment())
    assert not anonymous.can_resolve(comment())
