from __future__ import annotations

import sqlalchemy as sa
from obox_builder.db import JSONB, JsonDict, Model, TimestampMixin, relationship
from sqlalchemy.orm import Mapped, mapped_column

order_items = sa.Table(
    "shop_order_items",
    Model.metadata,
    sa.Column("order_id", sa.ForeignKey("shop_order.id"), primary_key=True),
    sa.Column("product_id", sa.ForeignKey("shop_product.id"), primary_key=True),
)


class Category(Model, TimestampMixin):
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(100))

    products: Mapped[list[Product]] = relationship(back_populates="category")


class Product(Model, TimestampMixin):
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(200))
    price_cents: Mapped[int] = mapped_column(sa.Integer, default=0)
    meta: Mapped[JsonDict] = mapped_column(JSONB, default=dict)
    category_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("shop_category.id"), nullable=True
    )

    category: Mapped[Category | None] = relationship(back_populates="products")

    __permissions__ = [("publish", "Can publish product")]


class Order(Model, TimestampMixin):
    id: Mapped[int] = mapped_column(primary_key=True)
    reference: Mapped[str] = mapped_column(sa.String(40), unique=True)
