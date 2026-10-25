"""Admin application: declarative ModelAdmin registration and generic CRUD."""

from __future__ import annotations

from typing import Any

from obox_builder.contrib.admin.actions import action, delete_selected
from obox_builder.contrib.admin.context import AdminContext
from obox_builder.contrib.admin.inlines import (
    InlineFormSet,
    InlineModelAdmin,
    StackedInline,
    TabularInline,
)
from obox_builder.contrib.admin.navigation import NavGroup, NavItem, build_navigation
from obox_builder.contrib.admin.options import AdminRoute, ModelAdmin, SimpleListFilter, display
from obox_builder.contrib.admin.pagination import AdminPage, AdminPaginator
from obox_builder.contrib.admin.site import AdminSite, site

__all__ = [
    "AdminContext",
    "AdminPage",
    "AdminPaginator",
    "AdminRoute",
    "AdminSite",
    "ModelAdmin",
    "SimpleListFilter",
    "InlineModelAdmin",
    "InlineFormSet",
    "StackedInline",
    "TabularInline",
    "NavGroup",
    "NavItem",
    "action",
    "build_navigation",
    "delete_selected",
    "display",
    "register",
    "site",
]


def register(model: type[Any], admin_class: type[ModelAdmin] | None = None) -> Any:
    """``@register(Model)`` / ``@register(Model, Admin)`` shortcut for ``site.register``."""
    return site.register(model, admin_class)
