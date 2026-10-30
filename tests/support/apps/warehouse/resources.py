from __future__ import annotations

from obox_builder.contrib.savedviews.resources import SavedViews

from obox_builder.resources import ModelResource, register

from .models import Contact, Crate, Item, Shelf, Tag


@register()
class ShelfResource(SavedViews, ModelResource[Shelf]):
    name = "warehouse.shelf"
    model = Shelf
    fields = ("id", "code", "created_at", "updated_at", "version")


@register()
class ItemResource(SavedViews, ModelResource[Item]):
    name = "warehouse.item"
    model = Item
    fields = (
        "id",
        "name",
        "sku",
        "quantity",
        "price",
        "released_on",
        "checked_at",
        "is_active",
        "notes",
        "meta",
        "internal_note",
        "shelf_id",
        "created_at",
        "updated_at",
        "version",
        "tags",
    )
    search_fields = ("name",)


@register()
class TagResource(SavedViews, ModelResource[Tag]):
    name = "warehouse.tag"
    model = Tag
    fields = ("id", "label", "created_at", "updated_at", "version", "items")


@register()
class ContactResource(SavedViews, ModelResource[Contact]):
    name = "warehouse.contact"
    model = Contact
    fields = ("id", "name", "email", "created_at", "updated_at", "version")


@register()
class CrateResource(SavedViews, ModelResource[Crate]):
    name = "warehouse.crate"
    model = Crate
    fields = ("id", "code", "weight_grams", "created_at", "updated_at")
