from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from obox_builder.db import JSONB, JsonDict, Model, TimestampMixin, VersionedMixin, relationship
from obox_builder.db.types import BigIntIdentity, TZDateTime
from sqlalchemy.orm import Mapped, mapped_column

item_tags = sa.Table(
    "obox_item_tags",
    Model.metadata,
    sa.Column("item_id", sa.ForeignKey("obox_item.id"), primary_key=True),
    sa.Column("tag_id", sa.ForeignKey("obox_tag.id"), primary_key=True),
)


class Shelf(Model, TimestampMixin, VersionedMixin):
    __history__ = True

    id: Mapped[int] = mapped_column(BigIntIdentity, primary_key=True)
    code: Mapped[str] = mapped_column(sa.String(20), unique=True)

    items: Mapped[list[Item]] = relationship(back_populates="shelf")

    def __str__(self) -> str:
        return self.code


class Item(Model, TimestampMixin, VersionedMixin):
    """Kitchen-sink tracked model exercising every field-kind diff."""

    __history__: Any = {"relations": ["tags"]}

    id: Mapped[int] = mapped_column(BigIntIdentity, primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(120))
    sku: Mapped[str] = mapped_column(sa.String(40), unique=True)
    quantity: Mapped[int] = mapped_column(sa.Integer, default=0)
    price: Mapped[Decimal | None] = mapped_column(sa.Numeric(10, 2), nullable=True)
    released_on: Mapped[date | None] = mapped_column(sa.Date, nullable=True)
    checked_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, default=True)
    notes: Mapped[str] = mapped_column(sa.Text, default="")
    meta: Mapped[JsonDict] = mapped_column(JSONB, default=dict)
    secret_token: Mapped[str] = mapped_column(
        sa.String(64), default="", info={"admin": {"secret": True}}
    )
    internal_note: Mapped[str] = mapped_column(
        sa.String(100), default="", info={"admin": {"history": False}}
    )
    shelf_id: Mapped[int | None] = mapped_column(sa.ForeignKey("obox_shelf.id"), nullable=True)

    shelf: Mapped[Shelf | None] = relationship(back_populates="items")
    tags: Mapped[list[Tag]] = relationship(secondary=item_tags, back_populates="items")

    def __str__(self) -> str:
        return self.name


class Tag(Model, TimestampMixin, VersionedMixin):
    __history__ = True

    id: Mapped[int] = mapped_column(BigIntIdentity, primary_key=True)
    label: Mapped[str] = mapped_column(sa.String(40), unique=True)

    items: Mapped[list[Item]] = relationship(secondary=item_tags, back_populates="tags")

    def __str__(self) -> str:
        return self.label


class Contact(Model, TimestampMixin, VersionedMixin):
    """Versioned tracked model with a partially masked field."""

    __history__: Any = {"mask": ["email"]}

    id: Mapped[int] = mapped_column(BigIntIdentity, primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(120))
    email: Mapped[str] = mapped_column(sa.String(254), default="")

    def __str__(self) -> str:
        return self.name


class Crate(Model, TimestampMixin):
    """Opt-in model: no declaration; tracked only via ``history.track``."""

    id: Mapped[int] = mapped_column(BigIntIdentity, primary_key=True)
    code: Mapped[str] = mapped_column(sa.String(40), unique=True)
    weight_grams: Mapped[int] = mapped_column(sa.Integer, default=0)

    def __str__(self) -> str:
        return self.code
