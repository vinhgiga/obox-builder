from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib import import_module
from importlib.util import find_spec
from pathlib import Path
from types import ModuleType
from typing import ClassVar

import sqlalchemy as sa

from obox_builder.core.exceptions import ImproperlyConfigured

LABEL_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


class AppConfig:
    name: str = ""
    label: str = ""
    verbose_name: str = ""
    default: bool = True
    permissions: ClassVar[list[str]] = []

    def __init__(self, name: str, module: ModuleType | None = None) -> None:
        if module is None:
            module = self._import_app_module(name)
        self.name = name
        self.module = module
        self._models_imported = False
        self.path = self._resolve_path(module, name)
        if not self.label:
            self.label = name.rpartition(".")[2]
        if not LABEL_PATTERN.match(self.label):
            raise ImproperlyConfigured(
                f"Invalid app label {self.label!r} for app {name!r}: labels must start with a "
                "lowercase letter and contain only lowercase letters, digits and underscores."
            )
        if not self.verbose_name:
            self.verbose_name = self.label.replace("_", " ").title()

    @classmethod
    def create(cls, entry: str) -> AppConfig:
        last_component = entry.rpartition(".")[2]
        if last_component[:1].isupper():
            return cls._create_from_class_path(entry)
        module = cls._import_app_module(entry)
        config_class = cls._find_config_class(entry)
        if config_class is None:
            return cls(entry, module)
        name = getattr(config_class, "name", "") or entry
        return config_class(name, module)

    @classmethod
    def _create_from_class_path(cls, entry: str) -> AppConfig:
        module_path, _, class_name = entry.rpartition(".")
        module = cls._import_app_module(module_path)
        config_class = getattr(module, class_name, None)
        if not (isinstance(config_class, type) and issubclass(config_class, AppConfig)):
            raise ImproperlyConfigured(f"{entry!r} does not point to an AppConfig subclass.")
        name = module_path.removesuffix(".apps")
        app_module = cls._import_app_module(name)
        name = getattr(config_class, "name", "") or name
        return config_class(name, app_module)

    @classmethod
    def _import_app_module(cls, name: str) -> ModuleType:
        try:
            return import_module(name)
        except ImportError as exc:
            raise ImproperlyConfigured(f"Cannot import app {name!r}: {exc}") from exc

    @classmethod
    def _find_config_class(cls, entry: str) -> type[AppConfig] | None:
        apps_module_path = f"{entry}.apps"
        try:
            apps_module = import_module(apps_module_path)
        except ModuleNotFoundError as exc:
            if exc.name == apps_module_path:
                return None
            raise ImproperlyConfigured(f"Error importing {apps_module_path}: {exc}") from exc
        except ImportError as exc:
            raise ImproperlyConfigured(f"Error importing {apps_module_path}: {exc}") from exc

        candidates = [
            value
            for value in vars(apps_module).values()
            if isinstance(value, type)
            and issubclass(value, cls)
            and value is not cls
            and value.default
        ]
        if len(candidates) > 1:
            names = ", ".join(sorted(candidate.__name__ for candidate in candidates))
            raise ImproperlyConfigured(
                f"{apps_module_path} defines more than one default AppConfig ({names}); "
                "mark all but one with default = False or reference the class directly."
            )
        return candidates[0] if candidates else None

    @staticmethod
    def _resolve_path(module: ModuleType, name: str) -> Path:
        module_file = getattr(module, "__file__", None)
        if module_file:
            return Path(module_file).resolve().parent
        module_paths = getattr(module, "__path__", None)
        if module_paths:
            first_path = next(iter(module_paths), None)
            if first_path:
                return Path(first_path).resolve()
        raise ImproperlyConfigured(f"App module {name!r} has no filesystem path.")

    def import_models(self) -> None:
        if self._models_imported:
            return
        models_path = f"{self.name}.models"
        try:
            spec = find_spec(models_path)
        except (ImportError, ValueError):
            spec = None
        if spec is None:
            self._models_imported = True
            return
        try:
            module = import_module(models_path)
        except ImportError as exc:
            raise ImproperlyConfigured(
                f"Error importing models for app {self.label!r}: {exc}"
            ) from exc
        self._register_module_models(module)
        self._models_imported = True

    def _register_module_models(self, module: ModuleType) -> None:
        """Register SQLAlchemy models and bare tables declared in ``models.py``."""
        from obox_builder.apps import apps
        from obox_builder.db.base import ModelBase

        for value in vars(module).values():
            if (
                isinstance(value, type)
                and issubclass(value, ModelBase)
                and value is not ModelBase
                and value.__module__ == module.__name__
                and not value.__dict__.get("__abstract__", False)
            ):
                apps.register_model(self.label, value)
                table = getattr(value, "__table__", None)
                if table is not None:
                    table.info["app_label"] = self.label
            elif isinstance(value, sa.Table):
                value.info["app_label"] = self.label
                apps.register_table(self.label, value)

    def ready(self) -> None:
        """Hook called once all app models have been imported."""

    @asynccontextmanager
    async def lifespan(self) -> AsyncIterator[None]:
        """Optional async resources, entered at ASGI startup and closed in reverse."""
        yield
