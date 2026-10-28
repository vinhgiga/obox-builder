from __future__ import annotations

from obox_builder.contrib.admin.navigation import (
    NavGroup,
    NavItem,
    build_navigation,
)
from obox_builder.contrib.auth.models import AnonymousUser


class _StubUser:
    def __init__(self, permissions: frozenset[str]) -> None:
        self.permissions = permissions

    def has_perm(self, perm: str) -> bool:
        return perm in self.permissions


def test_default_navigation_filters_by_permissions() -> None:
    anonymous = AnonymousUser()
    labels = [item.label for group in build_navigation(anonymous) for item in group.items]
    assert labels == ["Dashboard"]

    viewer = _StubUser(frozenset({"auth.users.read"}))
    labels = [item.label for group in build_navigation(viewer) for item in group.items]
    assert labels == ["Dashboard", "Users"]


def test_empty_groups_are_dropped() -> None:
    groups = [
        NavGroup(label="Overview", items=[NavItem("Dashboard", "/admin/")]),
        NavGroup(
            label="Access",
            items=[
                NavItem("Users", "/admin/users", required_perm="auth.users.read"),
                NavItem("Roles", "/admin/roles", required_perm="auth.roles.read"),
            ],
        ),
    ]
    anonymous = AnonymousUser()
    built = build_navigation(anonymous, groups)
    assert [group.label for group in built] == ["Overview"]


def test_custom_navigation_extends_defaults() -> None:
    groups = [
        NavGroup(
            label="Blog",
            items=[NavItem("Posts", "/admin/posts", required_perm="blog.post.read")],
        )
    ]
    admin = _StubUser(frozenset({"blog.post.read"}))
    built = build_navigation(admin, groups)
    assert [group.label for group in built] == ["Blog"]
    assert built[0].items[0].label == "Posts"
