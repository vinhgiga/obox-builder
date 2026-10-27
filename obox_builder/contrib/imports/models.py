from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from obox_builder.core.utils.time import utcnow
from obox_builder.db import Model
from obox_builder.db.types import JSONB, TZDateTime


class ImportSession(Model):
    app_label = "imports"
    id: Mapped[str] = mapped_column(
        sa.String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    owner_id: Mapped[int] = mapped_column(sa.Integer, index=True)
    model_label: Mapped[str] = mapped_column(sa.String(255))
    filename: Mapped[str] = mapped_column(sa.String(255))
    source: Mapped[bytes | None] = mapped_column(sa.LargeBinary, deferred=True)
    mode: Mapped[str] = mapped_column(sa.String(20))
    status: Mapped[str] = mapped_column(sa.String(20), default="uploaded", index=True)
    revision: Mapped[int] = mapped_column(default=0)
    task_id: Mapped[str | None] = mapped_column(sa.String(155))
    sheets: Mapped[list[str]] = mapped_column(JSONB, default=list)
    sample: Mapped[list[list[Any]]] = mapped_column(JSONB, default=list)
    sheet: Mapped[str] = mapped_column(sa.String(255), default="")
    header_row: Mapped[int] = mapped_column(default=1)
    headers: Mapped[list[str]] = mapped_column(JSONB, default=list)
    mapping: Mapped[dict[str, str]] = mapped_column(JSONB, default=dict)
    resource_signature: Mapped[str] = mapped_column(sa.String(64), default="")
    total: Mapped[int] = mapped_column(default=0)
    processed: Mapped[int] = mapped_column(default=0)
    error: Mapped[str] = mapped_column(sa.String(500), default="")
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow, index=True)

    __table_args__ = (
        sa.CheckConstraint(
            "mode IN ('insert', 'upsert', 'update-only', 'skip-existing')", name="mode"
        ),
        sa.CheckConstraint(
            "status IN ('uploaded', 'setup', 'parsing', 'mapping', 'validating', 'review', "
            "'queued', 'committing', 'completed', 'cancelled', 'failed')",
            name="status",
        ),
    )


class ImportRow(Model):
    app_label = "imports"
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[str] = mapped_column(
        sa.ForeignKey("obox_import_session.id", ondelete="CASCADE")
    )
    number: Mapped[int] = mapped_column()
    data: Mapped[list[Any]] = mapped_column(JSONB)
    key: Mapped[str | None] = mapped_column(sa.String(64))
    action: Mapped[str] = mapped_column(sa.String(20), default="pending")
    snapshot: Mapped[str | None] = mapped_column(sa.String(64))
    errors: Mapped[dict[str, list[str]]] = mapped_column(JSONB, default=dict)
    committed: Mapped[bool] = mapped_column(default=False)

    __table_args__ = (
        sa.UniqueConstraint("session_id", "number"),
        sa.Index("ix_import_rows_action", "session_id", "committed", "id"),
        sa.Index("ix_import_rows_key", "session_id", "key"),
    )
