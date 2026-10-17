"""Admin navigation: declarative items filtered by the current user's permissions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class PermissionHolder(Protocol):
    """Anything able to check ``app_label.codename`` permissions."""

    def has_perm(self, perm: str) -> bool: ...


@dataclass(frozen=True)
class NavItem:
    """A single sidebar link; hidden unless ``required_perm`` is held."""

    label: str
    url: str
    icon: str = ""
    required_perm: str | None = None

    def is_visible(self, user: PermissionHolder) -> bool:
        return self.required_perm is None or user.has_perm(self.required_perm)


@dataclass(frozen=True)
class NavGroup:
    """A labelled group of navigation items."""

    label: str
    items: list[NavItem] = field(default_factory=list)

    def visible_items(self, user: PermissionHolder) -> list[NavItem]:
        return [item for item in self.items if item.is_visible(user)]


def default_navigation() -> list[NavGroup]:
    return [
        NavGroup(
            label="Overview", items=[NavItem("Dashboard", "/admin/", icon="ti-layout-dashboard")]
        ),
        NavGroup(
            label="Access",
            items=[
                NavItem("Users", "/admin/users", icon="ti-users", required_perm="auth.view_user"),
                NavItem(
                    "Roles", "/admin/roles", icon="ti-shield-lock", required_perm="auth.view_role"
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
