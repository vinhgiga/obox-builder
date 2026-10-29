"""Celery-style task metadata with durable queue and lease extensions.

Metadata names and VARCHAR lengths follow Celery's SQLAlchemy TaskExtended model.
JSON replaces PickleType/LargeBinary; dates are timezone-aware UTC. Queue,
lease and attempt fields belong to this implementation, not Celery's backend.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from obox_builder.core.utils.time import utcnow
from obox_builder.db import Model
from obox_builder.db.types import JSONB, TZDateTime


class Task(Model):
    app_label = "jobs"

    id: Mapped[str] = mapped_column(sa.String(155), primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(155))
    queue: Mapped[str] = mapped_column(sa.String(155), default="default")
    args: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    kwargs: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(sa.String(50), default="PENDING")
    result: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    traceback: Mapped[str | None] = mapped_column(sa.Text)
    worker: Mapped[str | None] = mapped_column(sa.String(155), index=True)
    retries: Mapped[int] = mapped_column(sa.Integer, default=0)
    max_retries: Mapped[int] = mapped_column(sa.Integer, default=3)
    eta: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    date_created: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow, index=True)
    date_done: Mapped[datetime | None] = mapped_column(TZDateTime, index=True)
    lock_token: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    locked_until: Mapped[datetime | None] = mapped_column(TZDateTime)
    dedup_key: Mapped[str | None] = mapped_column(sa.String(255), unique=True)

    __table_args__ = (
        sa.CheckConstraint(
            "status IN ('PENDING', 'STARTED', 'RETRY', 'SUCCESS', 'FAILURE', 'REVOKED')",
            name="status",
        ),
        sa.CheckConstraint("retries >= 0 AND max_retries >= retries", name="retries"),
        sa.CheckConstraint(
            "(status = 'STARTED' AND lock_token IS NOT NULL AND locked_until IS NOT NULL) OR "
            "(status <> 'STARTED' AND lock_token IS NULL AND locked_until IS NULL)",
            name="lease",
        ),
        sa.Index("ix_jobs_task_due", "queue", "status", "eta", "date_created", "id"),
        sa.Index("ix_jobs_task_lease", "status", "locked_until"),
    )


class TaskAttempt(Model):
    app_label = "jobs"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    task_id: Mapped[str] = mapped_column(
        sa.String(155), sa.ForeignKey("obox_task.id", ondelete="CASCADE")
    )
    retries: Mapped[int] = mapped_column(sa.Integer)
    worker: Mapped[str] = mapped_column(sa.String(155))
    status: Mapped[str] = mapped_column(sa.String(50), default="STARTED")
    date_started: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    date_done: Mapped[datetime | None] = mapped_column(TZDateTime)
    traceback: Mapped[str | None] = mapped_column(sa.Text)

    __table_args__ = (
        sa.UniqueConstraint("task_id", "retries"),
        sa.CheckConstraint("retries >= 0", name="retries"),
        sa.CheckConstraint(
            "status IN ('STARTED', 'SUCCESS', 'FAILURE', 'ABANDONED', 'REVOKED')", name="status"
        ),
    )


class Worker(Model):
    app_label = "jobs"

    hostname: Mapped[str] = mapped_column(sa.String(155), primary_key=True)
    pid: Mapped[int] = mapped_column(sa.Integer)
    queues: Mapped[list[str]] = mapped_column(JSONB)
    concurrency: Mapped[int] = mapped_column(sa.Integer)
    started_at: Mapped[datetime] = mapped_column(TZDateTime)
    heartbeat_at: Mapped[datetime] = mapped_column(TZDateTime, index=True)
    stopped_at: Mapped[datetime | None] = mapped_column(TZDateTime)

    __table_args__ = (sa.CheckConstraint("concurrency >= 1", name="concurrency"),)
