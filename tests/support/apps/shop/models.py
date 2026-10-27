from __future__ import annotations

from uuid import UUID, uuid4

import sqlalchemy as sa
from obox_builder.db import JSONB, JsonDict, Model, TimestampMixin, relationship
from sqlalchemy.orm import Mapped, mapped_column

order_items = sa.Table(
    "obox_order_items",
    Model.metadata,
    sa.Column("order_id", sa.ForeignKey("obox_order.id"), primary_key=True),
    sa.Column("product_id", sa.ForeignKey("obox_product.id"), primary_key=True),
)

order_labels = sa.Table(
    "obox_order_labels",
    Model.metadata,
    sa.Column("order_id", sa.ForeignKey("obox_order.id"), primary_key=True),
    sa.Column("label_id", sa.ForeignKey("obox_label.id"), primary_key=True),
)


class Category(Model, TimestampMixin):
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(100))

    products: Mapped[list[Product]] = relationship(back_populates="category", passive_deletes=True)

    def __str__(self) -> str:
        return self.name


class Product(Model, TimestampMixin):
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(200), index=True)
    status: Mapped[str] = mapped_column(
        sa.String(20),
        index=True,
        default="draft",
        info={
            "admin": {
                "type": "single_select",
                "required": True,
                "choices": [("draft", "Draft"), ("live", "Live")],
            }
        },
    )
    price_cents: Mapped[int] = mapped_column(sa.Integer, default=0)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, index=True, default=True)
    meta: Mapped[JsonDict] = mapped_column(JSONB, default=dict)
    category_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("obox_category.id"), nullable=True
    )

    category: Mapped[Category | None] = relationship(back_populates="products")

    __permissions__ = [("publish", "Can publish product")]

    def __str__(self) -> str:
        return self.name


class Order(Model, TimestampMixin):
    id: Mapped[int] = mapped_column(primary_key=True)
    reference: Mapped[str] = mapped_column(sa.String(40), unique=True)
    labels: Mapped[list[Label]] = relationship(secondary=order_labels)
    lines: Mapped[list[OrderLine]] = relationship(
        back_populates="order", cascade="all, delete-orphan", passive_deletes=True
    )
    details: Mapped[OrderDetails | None] = relationship(
        back_populates="order", cascade="all, delete-orphan", uselist=False
    )

    def __str__(self) -> str:
        return self.reference


class Label(Model):
    id: Mapped[str] = mapped_column(sa.String(40), primary_key=True)
    title: Mapped[str] = mapped_column(sa.String(100), index=True)

    def __str__(self) -> str:
        return self.title


class OrderDetails(Model):
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(
        sa.ForeignKey("obox_order.id", ondelete="CASCADE"), unique=True
    )
    recipient: Mapped[str] = mapped_column(sa.String(80))
    order: Mapped[Order] = relationship(back_populates="details")

    def __str__(self) -> str:
        return self.recipient


class MultiTableRecord(Model):
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(80))


class DetailedMultiTableRecord(MultiTableRecord):
    __tablename__ = "obox_detailed_multi_table_record"

    id: Mapped[int] = mapped_column(
        sa.ForeignKey("obox_multi_table_record.id", ondelete="CASCADE"), primary_key=True
    )
    details: Mapped[str] = mapped_column(sa.String(120))


class Asset(Model):
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(sa.String(100), index=True)

    def __str__(self) -> str:
        return self.name


class OrderLine(Model):
    __table_args__ = (sa.UniqueConstraint("order_id", "sku"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(sa.ForeignKey("obox_order.id", ondelete="CASCADE"))
    product_id: Mapped[int | None] = mapped_column(sa.ForeignKey("obox_product.id"), nullable=True)
    asset_id: Mapped[UUID | None] = mapped_column(sa.ForeignKey("obox_asset.id"), nullable=True)
    sku: Mapped[str] = mapped_column(sa.String(40))
    quantity: Mapped[int] = mapped_column(default=1)
    order: Mapped[Order] = relationship(back_populates="lines")
    product: Mapped[Product | None] = relationship()
    asset: Mapped[Asset | None] = relationship()

    def __str__(self) -> str:
        return self.sku


class Shipment(Model):
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(sa.ForeignKey("obox_order.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(sa.String(40))

    def __str__(self) -> str:
        return self.name


class Tracking(Model):
    id: Mapped[int] = mapped_column(primary_key=True)
    shipment_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("obox_shipment.id", ondelete="SET NULL")
    )
    name: Mapped[str] = mapped_column(sa.String(40))

    def __str__(self) -> str:
        return self.name


class Invoice(Model):
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(sa.ForeignKey("obox_order.id", ondelete="RESTRICT"))
    name: Mapped[str] = mapped_column(sa.String(40))

    def __str__(self) -> str:
        return self.name
