from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JSONB = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
BigIntIdentity = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


class UTCDateTime(sa.TypeDecorator[datetime]):
    impl = sa.DateTime(timezone=True)
    cache_ok = True

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


TZDateTime = UTCDateTime()


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    metadata = sa.MetaData(
        naming_convention={
            "ix": "ix_%(table_name)s_%(column_0_N_name)s",
            "uq": "uq_%(table_name)s_%(column_0_N_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


class Task(Base):
    __tablename__ = "obox_task"

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
    expires: Mapped[datetime | None] = mapped_column(TZDateTime)
    priority: Mapped[int] = mapped_column(sa.Integer, default=0)
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
        sa.CheckConstraint("priority BETWEEN 0 AND 255", name="priority"),
        sa.CheckConstraint(
            "(status = 'STARTED' AND lock_token IS NOT NULL AND locked_until IS NOT NULL) OR "
            "(status <> 'STARTED' AND lock_token IS NULL AND locked_until IS NULL)",
            name="lease",
        ),
        sa.Index(
            "ix_jobs_task_due", "queue", "status", sa.desc("priority"), "eta", "date_created", "id"
        ),
        sa.Index("ix_jobs_task_expiry", "queue", "status", "expires"),
        sa.Index("ix_jobs_task_lease", "status", "locked_until"),
    )


class TaskAttempt(Base):
    __tablename__ = "obox_task_attempt"

    id: Mapped[int] = mapped_column(BigIntIdentity, primary_key=True)
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


class Worker(Base):
    __tablename__ = "obox_worker"

    hostname: Mapped[str] = mapped_column(sa.String(155), primary_key=True)
    pid: Mapped[int] = mapped_column(sa.Integer)
    queues: Mapped[list[str]] = mapped_column(JSONB)
    concurrency: Mapped[int] = mapped_column(sa.Integer)
    started_at: Mapped[datetime] = mapped_column(TZDateTime)
    heartbeat_at: Mapped[datetime] = mapped_column(TZDateTime, index=True)
    stopped_at: Mapped[datetime | None] = mapped_column(TZDateTime)

    __table_args__ = (sa.CheckConstraint("concurrency >= 1", name="concurrency"),)
