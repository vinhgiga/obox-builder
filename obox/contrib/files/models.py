"""Upload metadata; applications include Base.metadata in their Alembic environment."""

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    metadata = sa.MetaData(
        naming_convention={
            "pk": "pk_%(table_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "uq": "uq_%(table_name)s_%(column_0_N_name)s",
        }
    )


class File(Base):
    __tablename__ = "obox_file"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    key: Mapped[str] = mapped_column(sa.String(255), unique=True)
    filename: Mapped[str] = mapped_column(sa.String(255))
    content_type: Mapped[str] = mapped_column(sa.String(127))
    size: Mapped[int] = mapped_column(sa.BigInteger)
    offset: Mapped[int] = mapped_column(sa.BigInteger, default=0)
    status: Mapped[str] = mapped_column(sa.String(16), default="pending")
    owner_type: Mapped[str] = mapped_column(sa.String(255))
    field: Mapped[str] = mapped_column(sa.String(255))
    owner_id: Mapped[str | None] = mapped_column(sa.String(255))
    position: Mapped[int] = mapped_column(default=0, server_default="0")
    created_by: Mapped[str] = mapped_column(sa.String(255))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    completed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    unclaimed_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), default=lambda: datetime.now(UTC), server_default=sa.func.now()
    )

    __table_args__ = (
        sa.CheckConstraint("status IN ('pending', 'ready', 'failed')", name="status"),
        sa.CheckConstraint('size > 0 AND "offset" >= 0 AND "offset" <= size', name="size"),
        sa.Index("ix_obox_file_owner", "owner_type", "owner_id", "field"),
        sa.CheckConstraint("position >= 0", name="position"),
        sa.Index("ix_obox_file_gc", "owner_id", "unclaimed_at"),
    )
