"""Explicit, reusable business applications within one _Runtime runtime."""

from __future__ import annotations

import re
from copy import copy
from importlib import import_module
from importlib.resources import files
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request

from obox._i18n import LANGUAGES
from obox._pages import Page
from obox.core.exceptions import ImproperlyConfigured

if TYPE_CHECKING:
    from obox._components import Component
    from obox._config import RuntimeConfig
    from obox._context import Context
    from obox._runtime import _Runtime


class CatalogConfig:
    name: str = ""
    label: str = ""
    verbose_name: str = ""
    tab_bar_color: str = "#f3f4f6"
    requires: tuple[str, ...] = ()
    components: tuple[type[Component] | Component, ...] = ()
    pages: tuple[Page, ...] = ()
    ui_prefix: str | None = None
    routers: tuple[APIRouter, ...] = ()
    _navigation: Any = None

    @classmethod
    def load(cls, entry: str | type[CatalogConfig] | CatalogConfig) -> CatalogConfig:
        if isinstance(entry, str):
            module, _, attribute = entry.rpartition(".")
            try:
                entry = getattr(import_module(module), attribute)
            except (ImportError, AttributeError, ValueError) as exc:
                raise ImproperlyConfigured(f"Cannot load application {entry!r}.") from exc
        if isinstance(entry, type) and issubclass(entry, cls):
            entry = entry()
        if not isinstance(entry, cls):
            raise ImproperlyConfigured(
                "INSTALLED_APPS must contain CatalogConfig classes or paths."
            )
        config = copy(entry)
        if not isinstance(config.name, str) or not config.name:
            raise ImproperlyConfigured("CatalogConfig.name must identify its Python package.")
        config.label = config.label or config.name.rsplit(".", 1)[-1]
        if (
            not isinstance(config.label, str)
            or re.fullmatch(r"[a-z][a-z0-9_]*", config.label) is None
            or config.label in LANGUAGES
        ):
            raise ImproperlyConfigured("Application labels must be lowercase identifiers.")
        config.verbose_name = config.verbose_name or config.label.replace("_", " ").title()
        if not isinstance(config.verbose_name, str):
            raise ImproperlyConfigured("CatalogConfig.verbose_name must be a string.")
        if (
            not isinstance(config.tab_bar_color, str)
            or re.fullmatch(r"#[\da-fA-F]{6}", config.tab_bar_color) is None
        ):
            raise ImproperlyConfigured("CatalogConfig.tab_bar_color must be a six-digit hex color.")
        for field in ("requires", "components", "pages", "routers"):
            if not isinstance(getattr(config, field), tuple):
                raise ImproperlyConfigured(f"CatalogConfig.{field} must be a tuple.")
        if any(not isinstance(router, APIRouter) for router in config.routers):
            raise ImproperlyConfigured("CatalogConfig.routers must contain APIRouter instances.")
        if any(not isinstance(dependency, str) for dependency in config.requires):
            raise ImproperlyConfigured("Application dependencies must be strings.")
        try:
            package_module = import_module(config.name)
        except ImportError as exc:
            raise ImproperlyConfigured(f"Cannot import application {config.name!r}.") from exc
        if not hasattr(package_module, "__path__"):
            raise ImproperlyConfigured("CatalogConfig.name must identify a Python package.")
        return config

    def ui_path(self, settings: RuntimeConfig) -> str:
        path = (
            self.ui_prefix if self.ui_prefix is not None else f"{settings.ui_prefix}/{self.label}"
        )
        if (
            not isinstance(path, str)
            or not path.startswith("/")
            or path.endswith("/")
            or any(char in path for char in "{}?#")
        ):
            raise ImproperlyConfigured("Application UI prefixes must be absolute URL paths.")
        return path

    def has_directory(self, directory: str) -> bool:
        return files(self.name).joinpath(directory).is_dir()

    async def get_navigation(
        self, obox: _Runtime, request: Request, ctx: Context
    ) -> list[dict[str, Any]]:
        """Contribute custom pages; endpoints enforce their own authorization."""
        return await self._navigation(obox, request, ctx) if self._navigation else []
