"""Private, per-model list configurations owned by an admin user."""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from obox_builder.db import JSONB, Model
from obox_builder.db.types import BigIntIdentity, TZDateTime


class SavedView(Model):
    id: Mapped[int] = mapped_column(BigIntIdentity, primary_key=True)
    owner_id: Mapped[int] = mapped_column(
        BigIntIdentity, sa.ForeignKey("auth_user.id", ondelete="CASCADE"), nullable=False
    )
    site_name: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    model_label: Mapped[str] = mapped_column(sa.String(150), nullable=False)
    name: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False)

    __table_args__ = (
        sa.Index("ix_admin_saved_view_owner_model", "owner_id", "site_name", "model_label"),
        sa.CheckConstraint("length(name) BETWEEN 1 AND 100", name="name_length"),
    )


class ApiIdempotencyKey(Model):
    id: Mapped[int] = mapped_column(BigIntIdentity, primary_key=True)
    principal: Mapped[str] = mapped_column(sa.String(150))
    idempotency_key: Mapped[str] = mapped_column(sa.String(255))
    method: Mapped[str] = mapped_column(sa.String(10))
    path: Mapped[str] = mapped_column(sa.String(300))
    fingerprint: Mapped[str] = mapped_column(sa.String(64))
    response_status: Mapped[int] = mapped_column(sa.SmallInteger)
    response_body: Mapped[dict | list | None] = mapped_column(JSONB)
    response_headers: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(TZDateTime)
    expires_at: Mapped[datetime] = mapped_column(TZDateTime, index=True)

    __table_args__ = (sa.UniqueConstraint("principal", "idempotency_key"),)
