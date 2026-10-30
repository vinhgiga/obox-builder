"""Admin application: declarative ModelView registration and generic CRUD."""

from __future__ import annotations

from typing import Any

from obox_builder.contrib.admin.actions import delete_selected
from obox_builder.contrib.admin.context import AdminContext
from obox_builder.contrib.admin.inlines import (
    InlineFormSet,
    InlineModelView,
    StackedInline,
    TabularInline,
)
from obox_builder.contrib.admin.navigation import NavGroup, NavItem, build_navigation
from obox_builder.contrib.admin.options import AdminRoute, ModelView, SimpleListFilter, display
from obox_builder.contrib.admin.pagination import AdminPage, AdminPaginator
from obox_builder.contrib.admin.site import AdminSite, site

__all__ = [
    "AdminContext",
    "AdminPage",
    "AdminPaginator",
    "AdminRoute",
    "AdminSite",
    "ModelView",
    "SimpleListFilter",
    "InlineModelView",
    "InlineFormSet",
    "StackedInline",
    "TabularInline",
    "NavGroup",
    "NavItem",
    "build_navigation",
    "delete_selected",
    "display",
    "register",
    "site",
]


def register(model: type[Any], admin_class: type[ModelView] | None = None) -> Any:
    """``@register(Model)`` / ``@register(Model, Admin)`` shortcut for ``site.register``."""
    return site.view(model) if admin_class is None else site.register(model, admin_class)
