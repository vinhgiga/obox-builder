"""Admin navigation: declarative items filtered by the current user's permissions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from obox_builder.apps import apps


class PermissionHolder(Protocol):
    """Anything able to check ``service.resource.action`` permissions."""

    def has_perm(self, perm: str) -> bool: ...


@dataclass(frozen=True)
class NavItem:
    """A sidebar link requiring its primary permission or one of its alternatives."""

    label: str
    url: str
    icon: str = ""
    required_perm: str | None = None
    alternative_perms: tuple[str, ...] = ()

    def is_visible(self, user: PermissionHolder) -> bool:
        return (
            self.required_perm is None
            or user.has_perm(self.required_perm)
            or any(user.has_perm(perm) for perm in self.alternative_perms)
        )


@dataclass(frozen=True)
class NavGroup:
    """A labelled group of navigation items."""

    label: str
    items: list[NavItem] = field(default_factory=list)

    def visible_items(self, user: PermissionHolder) -> list[NavItem]:
        return [item for item in self.items if item.is_visible(user)]


def default_navigation() -> list[NavGroup]:
    user_view_permission = "auth.users.read"
    if apps.models_ready:
        from obox_builder.contrib.auth.models import USER_VIEW_PERMISSION

        user_view_permission = USER_VIEW_PERMISSION

    return [
        NavGroup(
            label="Overview", items=[NavItem("Dashboard", "/admin/", icon="ti-layout-dashboard")]
        ),
        NavGroup(
            label="Access",
            items=[
                NavItem(
                    "Users", "/admin/users", icon="ti-users", required_perm=user_view_permission
                ),
                NavItem(
                    "Roles", "/admin/roles", icon="ti-shield-lock", required_perm="auth.roles.read"
                ),
            ],
        ),
    ]


def build_navigation(
    user: PermissionHolder, groups: list[NavGroup] | None = None
) -> list[NavGroup]:
    """Return the groups with at least one visible item for ``user``."""
    source = groups if groups is not None else default_navigation()
    result = []
    for group in source:
        items = group.visible_items(user)
        if items:
            result.append(NavGroup(label=group.label, items=items))
    return result
