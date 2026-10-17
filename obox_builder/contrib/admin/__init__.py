"""Admin application: declarative ModelAdmin registration and generic CRUD."""

from __future__ import annotations

from typing import Any

from obox_builder.contrib.admin.context import AdminContext
from obox_builder.contrib.admin.navigation import NavGroup, NavItem, build_navigation
from obox_builder.contrib.admin.options import ModelAdmin
from obox_builder.contrib.admin.site import AdminSite, site

__all__ = [
    "AdminContext",
    "AdminSite",
    "ModelAdmin",
    "NavGroup",
    "NavItem",
    "build_navigation",
    "register",
    "site",
]


def register(model: type[Any], admin_class: type[ModelAdmin] | None = None) -> Any:
    """``@register(Model)`` / ``@register(Model, Admin)`` shortcut for ``site.register``."""
    return site.register(model, admin_class)
