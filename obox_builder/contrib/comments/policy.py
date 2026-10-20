"""The comment permission matrix (ADR-0002 section 7).

The policy is deliberately independent of the admin package: callers pass the
target model's ``view``/``change`` permission strings, which keeps this module
unit-testable and prevents an import cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from obox_builder.contrib.comments import (
    COMMENTS_ADD_PERMISSION,
    COMMENTS_MODERATE_PERMISSION,
    COMMENTS_VIEW_PERMISSION,
)
from obox_builder.contrib.comments.models import RowComment
from obox_builder.core.utils.time import ensure_aware, utcnow


class Viewer(Protocol):
    """Anything with a user id and permission checks (``User``/``AnonymousUser``)."""

    id: int | None

    def has_perm(self, perm: str) -> bool: ...


@dataclass(frozen=True)
class CommentPolicy:
    """Who may view, add, edit, delete or resolve comments on one model."""

    user: Viewer
    view_permission: str
    change_permission: str

    def _has(self, permission: str) -> bool:
        return bool(self.user.has_perm(permission))

    # -- capability gates ---------------------------------------------------
    def can_view(self) -> bool:
        return self._has(self.view_permission) and self._has(COMMENTS_VIEW_PERMISSION)

    def can_add(self) -> bool:
        return self._has(self.view_permission) and self._has(COMMENTS_ADD_PERMISSION)

    def can_moderate(self) -> bool:
        return self._has(self.view_permission) and self._has(COMMENTS_MODERATE_PERMISSION)

    def can_change_model(self) -> bool:
        return self._has(self.change_permission)

    # -- per-comment actions ------------------------------------------------
    def is_author(self, comment: RowComment) -> bool:
        return self.user.id is not None and comment.author_id == self.user.id

    def can_edit(
        self,
        comment: RowComment,
        *,
        edit_window_seconds: int | None = None,
        now: datetime | None = None,
    ) -> bool:
        if not self.can_add() or not self.is_author(comment) or comment.deleted_at is not None:
            return False
        if edit_window_seconds is None:
            return True
        created = ensure_aware(comment.created_at)
        return (now or utcnow()) - created <= timedelta(seconds=edit_window_seconds)

    def can_delete(self, comment: RowComment) -> bool:
        if comment.deleted_at is not None:
            return False
        return self.can_moderate() or (self.can_add() and self.is_author(comment))

    def can_resolve(self, comment: RowComment) -> bool:
        if comment.parent_id is not None or comment.deleted_at is not None:
            return False
        return self.can_add() and (
            self.is_author(comment) or self.can_change_model() or self.can_moderate()
        )

    def can_read_deleted(self) -> bool:
        return self.can_moderate()


__all__ = ["CommentPolicy", "Viewer"]
