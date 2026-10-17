"""The admin site object: holds navigation configuration and builds the router."""

from __future__ import annotations

from fastapi import APIRouter

from obox_builder.contrib.admin.navigation import NavGroup, default_navigation


class AdminSite:
    """Central admin configuration (ModelAdmins arrive in 0.2 via the registry)."""

    def __init__(self, name: str = "admin") -> None:
        self.name = name
        self.navigation: list[NavGroup] = default_navigation()

    def get_router(self) -> APIRouter:
        from obox_builder.contrib.admin.urls import build_router

        return build_router(self)


site = AdminSite()
