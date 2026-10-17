"""Admin registrations for the shop test app (loaded by autodiscovery)."""

from __future__ import annotations

from obox_builder.contrib.admin import ModelAdmin, register

from tests.support.apps.shop.models import Category, Order, Product


@register(Category)
class CategoryAdmin(ModelAdmin):
    list_display = ("id", "name")
    ordering = ("name",)


@register(Product)
class ProductAdmin(ModelAdmin):
    list_display = ("id", "name", "status", "price_cents", "category_id", "is_active")
    list_filter = ("status", "is_active")
    search_fields = ("name",)
    ordering = ("-id",)


@register(Order)
class OrderAdmin(ModelAdmin):
    list_display = ("id", "reference", "created_at")
    search_fields = ("reference",)
    ordering = ("-id",)
