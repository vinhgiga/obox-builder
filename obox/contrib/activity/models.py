"""Activity metadata; include Base.metadata in the host's Alembic environment."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    metadata = sa.MetaData(
        naming_convention={
            "pk": "pk_%(table_name)s",
            "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
            "uq": "uq_%(table_name)s_%(column_0_N_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
        }
    )


class Ownership:
    resource_key: Mapped[str] = mapped_column(sa.String(255))
    record_id: Mapped[str] = mapped_column(sa.String(255))


class Comment(Ownership, Base):
    __tablename__ = "obox_comment"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    author_id: Mapped[str] = mapped_column(sa.String(255))
    body: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    edited_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    version: Mapped[int] = mapped_column(default=1, server_default="1")

    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012

    __table_args__ = (
        sa.Index("ix_obox_comment_owner", "resource_key", "record_id"),
        sa.CheckConstraint(
            "(deleted_at IS NULL AND body IS NOT NULL) OR "
            "(deleted_at IS NOT NULL AND body IS NULL)",
            name="body",
        ),
    )


class Event(Ownership, Base):
    __tablename__ = "obox_activity"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True
    )
    kind: Mapped[str] = mapped_column(sa.String(16))
    action: Mapped[str | None] = mapped_column(sa.String(16))
    actor_id: Mapped[str | None] = mapped_column(sa.String(255))
    source: Mapped[str] = mapped_column(sa.String(16))
    occurred_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    changes: Mapped[list[dict[str, Any]] | None] = mapped_column(sa.JSON(none_as_null=True))
    comment_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("obox_comment.id", ondelete="CASCADE"), unique=True
    )

    __table_args__ = (
        sa.Index("ix_obox_activity_feed", "resource_key", "record_id", "id"),
        sa.CheckConstraint(
            "(kind = 'revision' AND action IN ('create', 'update', 'delete') "
            "AND action IS NOT NULL AND changes IS NOT NULL AND comment_id IS NULL) OR "
            "(kind = 'comment' AND action IS NULL AND changes IS NULL AND comment_id IS NOT NULL)",
            name="payload",
        ),
    )
