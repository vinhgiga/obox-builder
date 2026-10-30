"""Admin registrations for the shop test app (loaded by autodiscovery)."""

from __future__ import annotations

from obox_builder.contrib.admin import ModelView, register

from .resources import CategoryResource, OrderResource, ProductResource


@register(CategoryResource)
class CategoryAdmin(ModelView):
    list_display = ("id", "name")
    ordering = ("name",)


@register(ProductResource)
class ProductAdmin(ModelView):
    list_display = ("id", "name", "status", "price_cents", "category_id", "is_active")
    list_filter = ("status", "is_active")
    search_fields = ("name",)
    ordering = ("-id",)


@register(OrderResource)
class OrderAdmin(ModelView):
    list_display = ("id", "reference", "created_at")
    search_fields = ("reference",)
    ordering = ("-id",)
