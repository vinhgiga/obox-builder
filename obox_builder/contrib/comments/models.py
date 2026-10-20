"""Comment tables.

Comments are mutable user content, deliberately kept out of
``history_row_change`` (ADR-0002 D1). The polymorphic target uses the same
``(model_label, object_pk)`` identity as row history, so both features line up
without sharing a foreign key to every model.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, ClassVar

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from obox_builder.core.utils.time import utcnow
from obox_builder.db import Model, VersionedMixin
from obox_builder.db.types import BigIntIdentity, TZDateTime

BODY_FORMATS = ("text", "md")
SOURCES = ("admin_ui", "api", "cli", "job", "system", "import")

TABLE = "comments_row_comment"


class RowComment(Model, VersionedMixin):
    """A root comment or a one-level reply on one target row."""

    app_label = "comments"
    __comments__: ClassVar[bool | Mapping[str, Any] | None] = False
    __history__: ClassVar[bool | Mapping[str, Any] | None] = False

    id: Mapped[int] = mapped_column(BigIntIdentity, primary_key=True)
    model_label: Mapped[str] = mapped_column(sa.String(150), nullable=False)
    object_pk: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    object_repr: Mapped[str] = mapped_column(
        sa.String(255), default="", server_default="", nullable=False
    )
    parent_id: Mapped[int | None] = mapped_column(BigIntIdentity, default=None)
    body: Mapped[str | None] = mapped_column(sa.Text, default=None)
    body_format: Mapped[str] = mapped_column(
        sa.String(8), default="text", server_default="text", nullable=False
    )
    author_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("auth_user.id", ondelete="SET NULL"),
        default=None,
    )
    author_label: Mapped[str] = mapped_column(sa.String(150), nullable=False)
    source: Mapped[str] = mapped_column(
        sa.String(16), default="admin_ui", server_default="admin_ui", nullable=False
    )
    request_id: Mapped[str | None] = mapped_column(sa.String(64), default=None)
    idempotency_key: Mapped[str | None] = mapped_column(sa.String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow, nullable=False)
    edited_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    resolved_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    resolved_by_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("auth_user.id", ondelete="SET NULL"),
        default=None,
    )
    resolved_by_label: Mapped[str | None] = mapped_column(sa.String(150), default=None)
    deleted_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=None)
    deleted_by_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("auth_user.id", ondelete="SET NULL"),
        default=None,
    )
    deleted_by_label: Mapped[str | None] = mapped_column(sa.String(150), default=None)

    __table_args__ = (
        sa.CheckConstraint("body_format IN ('text', 'md')", name="body_format"),
        sa.CheckConstraint(
            "source IN ('admin_ui', 'api', 'cli', 'job', 'system', 'import')", name="source"
        ),
        sa.CheckConstraint("resolved_at IS NULL OR parent_id IS NULL", name="resolve_roots_only"),
        sa.CheckConstraint("deleted_at IS NOT NULL OR body IS NOT NULL", name="live_has_body"),
        sa.CheckConstraint("body IS NULL OR length(body) <= 20000", name="body_length"),
        # Anchor for the composite self-FK: a reply must live on the same target
        # row as its parent.
        sa.UniqueConstraint(
            "id", "model_label", "object_pk", name="uq_comments_row_comment_target"
        ),
        sa.ForeignKeyConstraint(
            ["parent_id", "model_label", "object_pk"],
            [
                f"{TABLE}.id",
                f"{TABLE}.model_label",
                f"{TABLE}.object_pk",
            ],
            ondelete="RESTRICT",
            name="fk_comments_row_comment_parent_same_target",
        ),
        sa.Index("ix_comments_row_comment_target", "model_label", "object_pk", "id"),
        sa.Index(
            "ix_comments_row_comment_replies",
            "parent_id",
            "id",
            postgresql_where=sa.text("parent_id IS NOT NULL"),
            sqlite_where=sa.text("parent_id IS NOT NULL"),
        ),
        sa.Index(
            "uq_comments_row_comment_idempotency",
            "author_id",
            "idempotency_key",
            unique=True,
            postgresql_where=sa.text("idempotency_key IS NOT NULL"),
            sqlite_where=sa.text("idempotency_key IS NOT NULL"),
        ),
        sa.Index("ix_comments_row_comment_author", "author_id", "id"),
        sa.Index(
            "ix_comments_row_comment_purge",
            "deleted_at",
            postgresql_where=sa.text("deleted_at IS NOT NULL"),
            sqlite_where=sa.text("deleted_at IS NOT NULL"),
        ),
    )

    def __repr__(self) -> str:
        return f"<RowComment {self.id} {self.model_label}#{self.object_pk}>"


class CommentMention(Model):
    """A mention token resolved to a user at write time (rename-safe)."""

    app_label = "comments"
    __comments__: ClassVar[bool | Mapping[str, Any] | None] = False
    __history__: ClassVar[bool | Mapping[str, Any] | None] = False

    comment_id: Mapped[int] = mapped_column(
        BigIntIdentity,
        sa.ForeignKey(f"{TABLE}.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[int] = mapped_column(
        BigIntIdentity,
        sa.ForeignKey("auth_user.id", ondelete="CASCADE"),
        primary_key=True,
    )

    __table_args__ = (
        sa.Index("ix_comments_comment_mention_user", "user_id", sa.desc("comment_id")),
    )

    def __repr__(self) -> str:
        return f"<CommentMention comment={self.comment_id} user={self.user_id}>"
