from __future__ import annotations

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import func
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from obox_builder.db.types import TZDateTime


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class VersionedMixin:
    """Optimistic locking via SQLAlchemy's ``version_id_col``.

    Every UPDATE carries ``WHERE version = <loaded value>`` and bumps the
    column, so two writers editing one row can no longer silently overwrite
    each other: the loser's flush raises ``StaleDataError``. Row history
    records the version into ``obox_row_change.row_version`` when the
    tracked model uses this mixin.
    """

    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("1"))

    @declared_attr.directive
    def __mapper_args__(cls: Any) -> dict[str, Any]:  # noqa: N805 - declared_attr
        return {
            "version_id_col": cls.__table__.c.version,
            "version_id_generator": lambda current: (current or 0) + 1,
        }
