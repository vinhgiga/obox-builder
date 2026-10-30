"""Persistent API replay claims; table identity is preserved across the refactor."""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from obox_builder.db import JSONB, Model
from obox_builder.db.types import BigIntIdentity, TZDateTime


class ApiIdempotencyKey(Model):
    app_label = "api"
    __tablename__ = "obox_api_idempotency_key"

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
