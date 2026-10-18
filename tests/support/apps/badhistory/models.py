from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from obox_builder.db import Model, relationship
from sqlalchemy.orm import Mapped, mapped_column


class Broken(Model):
    """Invalid ``__history__`` declaration: exercises E202/E203."""

    __history__: Any = {
        "fields": ["missing_field"],
        "exclude": ["also_missing"],
        "mask": ["nope"],
        "relations": ["made_up", "parent"],
    }

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(50), default="")
    parent_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("badhistory_broken.id"), nullable=True
    )

    parent: Mapped[Broken | None] = relationship(remote_side=[id], back_populates="children")
    children: Mapped[list[Broken]] = relationship(back_populates="parent")


class Pair(Model):
    """Composite primary key: declared but unsupported (E201)."""

    __history__ = True

    left: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    right: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    note: Mapped[str] = mapped_column(sa.String(100), default="")
