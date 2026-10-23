"""Private, per-model list configurations owned by an admin user."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from obox_builder.db import JSONB, Model
from obox_builder.db.types import BigIntIdentity


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
