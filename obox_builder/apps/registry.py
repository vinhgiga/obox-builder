from __future__ import annotations

import threading
from collections.abc import Iterable
from typing import Any

from obox_builder.apps.config import AppConfig
from obox_builder.core.exceptions import ImproperlyConfigured


class Apps:
    """Registry of installed applications, populated in three phases."""

    def __init__(self) -> None:
        self.app_configs: dict[str, AppConfig] = {}
        self.all_models: dict[str, dict[str, type[Any]]] = {}
        self.all_tables: dict[str, dict[str, Any]] = {}
        self.apps_ready = False
        self.models_ready = False
        self.ready = False
        self._lock = threading.RLock()

    def populate(self, installed_apps: Iterable[str]) -> None:
        with self._lock:
            if self.apps_ready or self.models_ready or self.ready:
                raise ImproperlyConfigured("Apps.populate() may only be called once.")
            app_configs = [AppConfig.create(entry) for entry in installed_apps]
            self._populate_app_configs(app_configs)
            self._populate_models()
            self._populate_ready()

    def _populate_app_configs(self, app_configs: list[AppConfig]) -> None:
        for app_config in app_configs:
            if app_config.label in self.app_configs:
                raise ImproperlyConfigured(
                    f"App label {app_config.label!r} is used by more than one installed app "
                    f"({self.app_configs[app_config.label].name!r} and {app_config.name!r})."
                )
            self.app_configs[app_config.label] = app_config
        self.apps_ready = True

    def _populate_models(self) -> None:
        for app_config in self.app_configs.values():
            self.all_models.setdefault(app_config.label, {})
            app_config.import_models()
        self.models_ready = True

    def _populate_ready(self) -> None:
        for app_config in self.app_configs.values():
            app_config.ready()
        self.ready = True

    def check_apps_ready(self) -> None:
        if not self.apps_ready:
            raise ImproperlyConfigured("Apps registry is not ready; call apps.populate().")

    def check_models_ready(self) -> None:
        if not self.models_ready:
            raise ImproperlyConfigured("Apps registry models are not ready; call apps.populate().")

    def get_app_config(self, label: str) -> AppConfig:
        self.check_apps_ready()
        try:
            return self.app_configs[label]
        except KeyError:
            raise LookupError(f"No installed app with label {label!r}.") from None

    def get_app_configs(self) -> list[AppConfig]:
        self.check_apps_ready()
        return list(self.app_configs.values())

    def get_models(self) -> list[type[Any]]:
        self.check_models_ready()
        return [model for models in self.all_models.values() for model in models.values()]

    def get_model(self, app_label: str, model_name: str) -> type[Any]:
        self.check_models_ready()
        try:
            return self.all_models[app_label][model_name.lower()]
        except KeyError:
            raise LookupError(
                f"App {app_label!r} doesn't have a model named {model_name!r}."
            ) from None

    def register_model(self, app_label: str, model: type[Any]) -> None:
        if not self.apps_ready:
            raise ImproperlyConfigured("register_model() called before apps.populate().")
        if self.models_ready:
            raise ImproperlyConfigured(
                "register_model() called after the models phase; import models from apps.py "
                "or models.py only."
            )
        model_name = model.__name__.lower()
        self.all_models.setdefault(app_label, {})[model_name] = model

    def register_table(self, app_label: str, table: Any) -> None:
        """Register a bare ``Table`` (association table) of an installed app."""
        if not self.apps_ready:
            raise ImproperlyConfigured("register_table() called before apps.populate().")
        if self.models_ready:
            raise ImproperlyConfigured(
                "register_table() called after the models phase; declare tables in models.py."
            )
        self.all_tables.setdefault(app_label, {})[table.name] = table

    def get_tables(self) -> list[Any]:
        self.check_models_ready()
        return [table for tables in self.all_tables.values() for table in tables.values()]

    def is_installed(self, app_name: str) -> bool:
        self.check_apps_ready()
        return any(config.name == app_name for config in self.app_configs.values())

    def get_containing_app_config(self, object_name: str) -> AppConfig | None:
        self.check_apps_ready()
        matches = [
            config
            for config in self.app_configs.values()
            if object_name == config.name or object_name.startswith(f"{config.name}.")
        ]
        if not matches:
            return None
        return max(matches, key=lambda config: len(config.name))

    def _reset(self) -> None:
        with self._lock:
            self.app_configs = {}
            self.all_models = {}
            self.all_tables = {}
            self.apps_ready = False
            self.models_ready = False
            self.ready = False
