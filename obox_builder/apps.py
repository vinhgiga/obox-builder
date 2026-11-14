"""Explicit, reusable business applications within one Obox runtime."""

from __future__ import annotations

import re
from copy import copy
from importlib import import_module
from importlib.resources import files
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request

from obox_builder.errors import ImproperlyConfigured
from obox_builder.i18n import LANGUAGES
from obox_builder.pages import Page

if TYPE_CHECKING:
    from obox_builder.application import Obox
    from obox_builder.builder import ModelBuilder
    from obox_builder.conf import Settings
    from obox_builder.context import Context
    from obox_builder.contrib.analytics import Dashboard


class AppConfig:
    name: str = ""
    label: str = ""
    verbose_name: str = ""
    tab_bar_color: str = "#f3f4f6"
    requires: tuple[str, ...] = ()
    builders: tuple[type[ModelBuilder[Any]], ...] = ()
    dashboards: tuple[Dashboard, ...] = ()
    pages: tuple[Page, ...] = ()
    ui_prefix: str | None = None
    ui_router: APIRouter | None = None
    api_router: APIRouter | None = None

    @classmethod
    def load(cls, entry: str | type[AppConfig] | AppConfig) -> AppConfig:
        if isinstance(entry, str):
            module, _, attribute = entry.rpartition(".")
            try:
                entry = getattr(import_module(module), attribute)
            except (ImportError, AttributeError, ValueError) as exc:
                raise ImproperlyConfigured(f"Cannot load application {entry!r}.") from exc
        if isinstance(entry, type) and issubclass(entry, cls):
            entry = entry()
        if not isinstance(entry, cls):
            raise ImproperlyConfigured("INSTALLED_APPS must contain AppConfig classes or paths.")
        config = copy(entry)
        if not isinstance(config.name, str) or not config.name:
            raise ImproperlyConfigured("AppConfig.name must identify its Python package.")
        config.label = config.label or config.name.rsplit(".", 1)[-1]
        if (
            not isinstance(config.label, str)
            or re.fullmatch(r"[a-z][a-z0-9_]*", config.label) is None
            or config.label in LANGUAGES
        ):
            raise ImproperlyConfigured("Application labels must be lowercase identifiers.")
        config.verbose_name = config.verbose_name or config.label.replace("_", " ").title()
        if not isinstance(config.verbose_name, str):
            raise ImproperlyConfigured("AppConfig.verbose_name must be a string.")
        if (
            not isinstance(config.tab_bar_color, str)
            or re.fullmatch(r"#[\da-fA-F]{6}", config.tab_bar_color) is None
        ):
            raise ImproperlyConfigured("AppConfig.tab_bar_color must be a six-digit hex color.")
        for field in ("requires", "builders", "dashboards", "pages"):
            if not isinstance(getattr(config, field), tuple):
                raise ImproperlyConfigured(f"AppConfig.{field} must be a tuple.")
        if any(not isinstance(dependency, str) for dependency in config.requires):
            raise ImproperlyConfigured("Application dependencies must be strings.")
        try:
            package_module = import_module(config.name)
        except ImportError as exc:
            raise ImproperlyConfigured(f"Cannot import application {config.name!r}.") from exc
        if not hasattr(package_module, "__path__"):
            raise ImproperlyConfigured("AppConfig.name must identify a Python package.")
        return config

    def ui_path(self, settings: Settings) -> str:
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
        self, obox: Obox, request: Request, ctx: Context
    ) -> list[dict[str, Any]]:
        """Contribute custom pages; endpoints enforce their own authorization."""
        return []
