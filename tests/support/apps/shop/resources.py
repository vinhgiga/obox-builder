from __future__ import annotations

from obox_builder.contrib.savedviews.resources import SavedViews

from obox_builder.resources import ModelResource, OperationContext, Related, operation, register
from pydantic import BaseModel

from .models import (
    Asset,
    Category,
    DetailedMultiTableRecord,
    Invoice,
    Label,
    MultiTableRecord,
    Order,
    OrderDetails,
    OrderLine,
    Product,
    Shipment,
    Tracking,
)


@register()
class CategoryResource(SavedViews, ModelResource[Category]):
    name = "shop.category"
    model = Category
    fields = ("id", "name", "created_at", "updated_at")
    ordering = ("name",)


class Published(BaseModel):
    affected: int


@register()
class ProductResource(SavedViews, ModelResource[Product]):
    name = "shop.product"
    model = Product
    import_keys = ("id",)
    fields = (
        "id",
        "name",
        "status",
        "price_cents",
        "is_active",
        "meta",
        "category_id",
        "created_at",
        "updated_at",
    )
    search_fields = ("name",)
    ordering = ("-id",)

    @operation(scope="selection", label="Can publish products")
    async def publish(self, ctx: OperationContext, objects: list[Product]) -> Published:
        for obj in objects:
            obj.status = "live"
        await ctx.session.flush()
        return Published(affected=len(objects))


@register()
class OrderResource(SavedViews, ModelResource[Order]):
    name = "shop.order"
    model = Order
    import_keys = ("reference",)
    fields = ("id", "reference", "created_at", "updated_at", "labels")
    search_fields = ("reference",)
    ordering = ("-id",)
    related = {
        "lines": Related("shop.orderLine", "order_id"),
        "details": Related("shop.orderDetails", "order_id", max_num=1),
    }


@register()
class LabelResource(SavedViews, ModelResource[Label]):
    name = "shop.label"
    model = Label
    fields = ("id", "title")


@register()
class OrderDetailsResource(SavedViews, ModelResource[OrderDetails]):
    name = "shop.orderDetails"
    model = OrderDetails
    fields = ("id", "order_id", "recipient")


@register()
class MultiTableRecordResource(SavedViews, ModelResource[MultiTableRecord]):
    name = "shop.multiTableRecord"
    model = MultiTableRecord
    fields = ("id", "name")


@register()
class DetailedMultiTableRecordResource(SavedViews, ModelResource[DetailedMultiTableRecord]):
    name = "shop.detailedMultiTableRecord"
    model = DetailedMultiTableRecord
    fields = ("id", "name", "details")


@register()
class AssetResource(SavedViews, ModelResource[Asset]):
    name = "shop.asset"
    model = Asset
    fields = ("id", "name")


@register()
class OrderLineResource(SavedViews, ModelResource[OrderLine]):
    name = "shop.orderLine"
    model = OrderLine
    fields = ("id", "order_id", "product_id", "asset_id", "sku", "quantity")


@register()
class ShipmentResource(SavedViews, ModelResource[Shipment]):
    name = "shop.shipment"
    model = Shipment
    fields = ("id", "order_id", "name")


@register()
class TrackingResource(SavedViews, ModelResource[Tracking]):
    name = "shop.tracking"
    model = Tracking
    fields = ("id", "shipment_id", "name")


@register()
class InvoiceResource(SavedViews, ModelResource[Invoice]):
    name = "shop.invoice"
    model = Invoice
    fields = ("id", "order_id", "name")
