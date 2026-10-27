"""Immutable event envelopes and durable per-subscription delivery state."""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from obox_builder.core.utils.time import utcnow
from obox_builder.db import Model
from obox_builder.db.types import JSONB, BigIntIdentity, JsonDict, TZDateTime


class Event(Model):
    app_label = "events"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True)
    type: Mapped[str] = mapped_column(sa.String(150), index=True)
    occurred_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow, index=True)
    payload: Mapped[JsonDict] = mapped_column(JSONB)
    schema_version: Mapped[int] = mapped_column(default=1)
    subject: Mapped[str | None] = mapped_column(sa.String(512))
    dedup_key: Mapped[str | None] = mapped_column(sa.String(255), unique=True)
    changed_fields: Mapped[list[str]] = mapped_column(JSONB, default=list)
    source: Mapped[str] = mapped_column(sa.String(32), default="system")
    actor_id: Mapped[int | None] = mapped_column(sa.BigInteger)
    actor_label: Mapped[str] = mapped_column(sa.String(150), default="system")
    request_id: Mapped[str | None] = mapped_column(sa.String(64))
    causation_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    producer: Mapped[str | None] = mapped_column(sa.String(255))
    hop: Mapped[int] = mapped_column(default=0)

    __table_args__ = (sa.CheckConstraint("schema_version >= 1 AND hop >= 0", name="version_hop"),)


class Delivery(Model):
    app_label = "events"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True)
    event_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("obox_event.id"), index=True)
    subscription_id: Mapped[str] = mapped_column(sa.String(255))
    queue: Mapped[str] = mapped_column(sa.String(150), default="default")
    status: Mapped[str] = mapped_column(sa.String(16), default="pending")
    attempts: Mapped[int] = mapped_column(default=0)
    max_attempts: Mapped[int] = mapped_column(default=8)
    run_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    locked_by: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    locked_until: Mapped[datetime | None] = mapped_column(TZDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    last_error: Mapped[str | None] = mapped_column(sa.String(255))

    __table_args__ = (
        sa.UniqueConstraint("event_id", "subscription_id"),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'dead', 'discarded')", name="status"
        ),
        sa.CheckConstraint("attempts >= 0 AND max_attempts >= 1", name="attempts"),
        sa.Index("ix_events_delivery_due", "queue", "status", "run_at"),
        sa.Index("ix_events_delivery_lease", "status", "locked_until"),
    )


class DeliveryAttempt(Model):
    app_label = "events"

    id: Mapped[int] = mapped_column(BigIntIdentity, primary_key=True)
    delivery_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("obox_delivery.id"), index=True)
    number: Mapped[int] = mapped_column()
    started_at: Mapped[datetime] = mapped_column(TZDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    outcome: Mapped[str] = mapped_column(sa.String(16), default="running")
    error: Mapped[str | None] = mapped_column(sa.String(255))

    __table_args__ = (sa.UniqueConstraint("delivery_id", "number"),)
