"""Include Base.metadata in the host's migrations when enabling idempotency."""

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Receipt(Base):
    __tablename__ = "obox_idempotency"

    key: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(sa.String(64))
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), index=True)
    response: Mapped[dict[str, Any] | None] = mapped_column(sa.JSON(none_as_null=True))
