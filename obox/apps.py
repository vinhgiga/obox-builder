"""Application discovery and the config → models → ready lifecycle."""

from __future__ import annotations

from importlib import import_module
from importlib.util import find_spec
from pathlib import Path
from threading import RLock
from types import ModuleType
from typing import Any

from obox.core.exceptions import AppRegistryNotReady, ImproperlyConfigured


class AppConfig:
    def __init__(self, app_name: str, app_module: ModuleType) -> None:
        self.name, self.module = app_name, app_module
        module_file = getattr(app_module, "__file__", None)
        if module_file is not None:
            self.path = str(Path(module_file).resolve().parent)
        else:
            locations = iter(getattr(app_module, "__path__", ()))
            location = next(locations, None)
            if location is None:
                raise ImproperlyConfigured(f"Application {app_name!r} has no package path.")
            self.path = str(Path(location).resolve())
        self.label = getattr(self, "label", app_name.rsplit(".", 1)[-1])
        if not isinstance(self.label, str) or not self.label.isidentifier():
            raise ImproperlyConfigured(f"Invalid application label: {self.label!r}.")
        self.verbose_name = getattr(self, "verbose_name", self.label.replace("_", " ").title())
        self.models_module: ModuleType | None = None
        self.models: dict[str, type[Any]] = {}
        self.apps: Apps

    @classmethod
    def create(cls, entry: str) -> AppConfig:
        if not isinstance(entry, str):
            raise ImproperlyConfigured("INSTALLED_APPS entries must be module or AppConfig paths.")
        try:
            module = import_module(entry)
        except ModuleNotFoundError as exc:
            # Do not mask a missing dependency inside an existing app.
            if exc.name != entry and not entry.startswith(str(exc.name) + "."):
                raise
            package, _, name = entry.rpartition(".")
            if not package:
                raise
            config = getattr(import_module(package), name)
            if not isinstance(config, type) or not issubclass(config, cls):
                raise ImproperlyConfigured(f"{entry!r} is not an AppConfig class.") from exc
            app_name = getattr(config, "name", None)
            if not app_name:
                raise ImproperlyConfigured(f"{entry!r} must define name.") from exc
            return config(app_name, import_module(app_name))
        config_class: type[AppConfig] = cls
        spec = find_spec(entry + ".apps") if hasattr(module, "__path__") else None
        if spec is not None:
            candidates = [
                value
                for value in vars(import_module(entry + ".apps")).values()
                if isinstance(value, type)
                and issubclass(value, cls)
                and value is not cls
                and getattr(value, "default", True)
            ]
            candidates = list(dict.fromkeys(candidates))
            if len(candidates) > 1:
                candidates = [item for item in candidates if getattr(item, "default", False)]
                if len(candidates) > 1:
                    raise RuntimeError(f"More than one default AppConfig in {entry!r}.")
            if candidates:
                config_class = candidates[0]
        return config_class(getattr(config_class, "name", entry), module)

    def import_models(self) -> None:
        if hasattr(self.module, "__path__") and find_spec(self.name + ".models") is not None:
            self.models_module = import_module(self.name + ".models")
            import sqlalchemy as sa
            from sqlalchemy.exc import NoInspectionAvailable
            from sqlalchemy.orm import Mapper

            for model in vars(self.models_module).values():
                if not isinstance(model, type) or not model.__module__.startswith(self.name + "."):
                    continue
                try:
                    mapper: Any = sa.inspect(model)
                except NoInspectionAvailable:
                    continue
                if isinstance(mapper, Mapper):
                    self.models[model.__name__.lower()] = model

    def get_model(self, model_name: str, require_ready: bool = True) -> type[Any]:
        if require_ready:
            self.apps.check_models_ready()
        else:
            self.apps.check_apps_ready()
        try:
            return self.models[model_name.lower()]
        except KeyError as exc:
            raise LookupError(f"App {self.label!r} has no {model_name!r} model.") from exc

    def ready(self) -> None:
        pass


class Apps:
    def __init__(self) -> None:
        self.app_configs: dict[str, AppConfig] = {}
        self.apps_ready = self.models_ready = self.ready = self.loading = False
        self._lock = RLock()

    def populate(self, installed_apps: Any = None) -> None:
        with self._lock:
            if self.ready:
                return
            if self.loading:
                raise RuntimeError("populate() isn't reentrant")
            self.loading = True
            if installed_apps is None:
                from obox.conf import settings

                installed_apps = settings.INSTALLED_APPS
            for entry in installed_apps:
                config = entry if isinstance(entry, AppConfig) else AppConfig.create(entry)
                if config.label in self.app_configs:
                    raise ImproperlyConfigured(f"Application labels aren't unique: {config.label}.")
                if any(item.name == config.name for item in self.app_configs.values()):
                    raise ImproperlyConfigured(f"Application names aren't unique: {config.name}.")
                self.app_configs[config.label] = config
                config.apps = self
            self.apps_ready = True
            for config in self.app_configs.values():
                config.import_models()
            self.models_ready = True
            for config in self.app_configs.values():
                config.ready()
            self.ready = True

    def check_apps_ready(self) -> None:
        if not self.apps_ready:
            raise AppRegistryNotReady("Apps aren't loaded yet.")

    def check_models_ready(self) -> None:
        if not self.models_ready:
            raise AppRegistryNotReady("Models aren't loaded yet.")

    def get_app_configs(self) -> Any:
        self.check_apps_ready()
        return self.app_configs.values()

    def get_app_config(self, app_label: str) -> AppConfig:
        self.check_apps_ready()
        try:
            return self.app_configs[app_label]
        except KeyError as exc:
            raise LookupError(f"No installed app with label {app_label!r}.") from exc

    def get_model(
        self, app_label: str, model_name: str | None = None, require_ready: bool = True
    ) -> type[Any]:
        if model_name is None:
            app_label, model_name = app_label.split(".")
        config = self.get_app_config(app_label)
        if not require_ready and config.models_module is None:
            config.import_models()
        return config.get_model(model_name, require_ready)

    def is_installed(self, app_name: str) -> bool:
        self.check_apps_ready()
        return any(config.name == app_name for config in self.app_configs.values())


apps = Apps()
