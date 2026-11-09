"""Include Base.metadata in the host application's migrations."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    metadata = sa.MetaData(
        naming_convention={
            "pk": "pk_%(table_name)s",
            "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
            "uq": "uq_%(table_name)s_%(column_0_N_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "ix": "ix_%(table_name)s_%(column_0_N_name)s",
        }
    )


class Subscription(Base):
    __tablename__ = "obox_webhook_subscription"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[str] = mapped_column(sa.String(255), index=True)
    resource: Mapped[str] = mapped_column(sa.String(255), index=True)
    url: Mapped[str] = mapped_column(sa.Text)
    actions: Mapped[list[str]] = mapped_column(sa.JSON)
    key_id: Mapped[str] = mapped_column(sa.String(255))
    nonce: Mapped[str] = mapped_column(sa.String(64))
    enabled: Mapped[bool] = mapped_column(default=True)
    sequence: Mapped[int] = mapped_column(sa.BigInteger, default=0)
    slot: Mapped[int | None]
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))

    __table_args__ = (
        sa.UniqueConstraint("owner_id", "slot"),
        sa.CheckConstraint("slot > 0 AND sequence >= 0", name="sequence"),
    )


class Event(Base):
    __tablename__ = "obox_webhook_event"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    resource: Mapped[str] = mapped_column(sa.String(255))
    action: Mapped[str] = mapped_column(sa.String(16))
    record_id: Mapped[str] = mapped_column(sa.String(255))
    occurred_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), default=now, index=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(sa.JSON)

    __table_args__ = (sa.CheckConstraint("action IN ('create','update')", name="action"),)


class Delivery(Base):
    __tablename__ = "obox_webhook_delivery"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    subscription_id: Mapped[UUID] = mapped_column(sa.ForeignKey(Subscription.id), index=True)
    event_id: Mapped[UUID] = mapped_column(sa.ForeignKey(Event.id), index=True)
    sequence: Mapped[int] = mapped_column(sa.BigInteger)
    status: Mapped[str] = mapped_column(sa.String(16), default="pending")
    attempts: Mapped[int] = mapped_column(default=0)
    due_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=now)
    lock_token: Mapped[UUID | None]
    locked_until: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_status: Mapped[int | None]
    last_error: Mapped[str | None] = mapped_column(sa.String(64))
    completed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    __table_args__ = (
        sa.UniqueConstraint("subscription_id", "sequence"),
        sa.UniqueConstraint("subscription_id", "event_id"),
        sa.CheckConstraint(
            "status IN ('pending','sending','retry','delivered','failed','skipped')",
            name="status",
        ),
        sa.CheckConstraint("attempts >= 0 AND sequence > 0", name="sequence"),
        sa.CheckConstraint(
            "(status = 'sending' AND lock_token IS NOT NULL AND locked_until IS NOT NULL) OR "
            "(status <> 'sending' AND lock_token IS NULL AND locked_until IS NULL)",
            name="lease",
        ),
        sa.Index("ix_obox_webhook_due", "status", "due_at", "locked_until"),
    )
