from __future__ import annotations

import os
from importlib import import_module
from types import ModuleType
from typing import Any

from pydantic import ValidationError

from obox_builder.conf.base import BaseSettings
from obox_builder.core.exceptions import ImproperlyConfigured

SETTINGS_MODULE_ENV = "OBOX_SETTINGS_MODULE"

__all__ = ["SETTINGS_MODULE_ENV", "BaseSettings", "LazySettings", "settings"]


class LazySettings:
    """Settings proxy; the settings module is imported on first attribute access."""

    def __init__(self) -> None:
        self._wrapped: BaseSettings | None = None

    @property
    def configured(self) -> bool:
        return self._wrapped is not None

    def load(self) -> BaseSettings:
        wrapped = self._wrapped
        if wrapped is None:
            self.configure()
            wrapped = self._wrapped
        if wrapped is None:
            raise ImproperlyConfigured("Settings are not configured.")
        return wrapped

    def reset(self) -> None:
        """Forget the loaded settings so the next access reloads them."""
        self._wrapped = None

    def configure(
        self,
        settings: BaseSettings | type[BaseSettings] | str | None = None,
    ) -> None:
        if settings is None:
            self._wrapped = self._load_from_module(os.environ.get(SETTINGS_MODULE_ENV))
        elif isinstance(settings, BaseSettings):
            self._wrapped = settings
        elif isinstance(settings, type) and issubclass(settings, BaseSettings):
            self._wrapped = settings()  # pyright: ignore[reportCallIssue]
        elif isinstance(settings, str):
            self._wrapped = self._load_from_module(settings)
        else:
            raise TypeError(
                "configure() accepts a BaseSettings instance, a BaseSettings subclass, "
                f"a dotted module path or None, not {type(settings).__name__}"
            )

    def _load_from_module(self, dotted_path: str | None) -> BaseSettings:
        if not dotted_path:
            raise ImproperlyConfigured(
                "Settings are not configured. Set the "
                f"{SETTINGS_MODULE_ENV} environment variable to the dotted path of your "
                "settings module (for example 'config.settings')."
            )
        module = self._import_module(dotted_path)
        settings_class = self._find_settings_class(module, dotted_path)
        self._reject_unknown_env_vars(settings_class, dotted_path)
        try:
            return settings_class()  # pyright: ignore[reportCallIssue]
        except ValidationError as exc:
            raise ImproperlyConfigured(f"Invalid settings in {dotted_path!r}: {exc}") from exc

    @staticmethod
    def _reject_unknown_env_vars(settings_class: type[BaseSettings], dotted_path: str) -> None:
        allowed = {f"OBOX_{name}".upper() for name in settings_class.model_fields}
        allowed.add(SETTINGS_MODULE_ENV)
        unknown = sorted(
            key
            for key in os.environ
            if key.upper().startswith("OBOX_") and key.upper() not in allowed
        )
        if unknown:
            raise ImproperlyConfigured(
                f"Invalid settings in {dotted_path!r}: unknown environment variable(s) "
                f"{', '.join(unknown)}. Valid variables are the OBOX_ prefixed field "
                "names (for example OBOX_SECRET_KEY)."
            )

    @staticmethod
    def _import_module(dotted_path: str) -> ModuleType:
        try:
            return import_module(dotted_path)
        except ImportError as exc:
            raise ImproperlyConfigured(
                f"Could not import settings module {dotted_path!r}: {exc}"
            ) from exc

    @staticmethod
    def _find_settings_class(module: ModuleType, dotted_path: str) -> type[BaseSettings]:
        candidate = getattr(module, "Settings", None)
        if candidate is None:
            candidates = [
                value
                for value in vars(module).values()
                if isinstance(value, type)
                and issubclass(value, BaseSettings)
                and value is not BaseSettings
            ]
            if len(candidates) == 1:
                candidate = candidates[0]
            elif len(candidates) > 1:
                raise ImproperlyConfigured(
                    f"Settings module {dotted_path!r} defines multiple BaseSettings "
                    "subclasses; name the one to use 'Settings'."
                )
            else:
                raise ImproperlyConfigured(
                    f"Settings module {dotted_path!r} does not define a 'Settings' class."
                )
        if not (isinstance(candidate, type) and issubclass(candidate, BaseSettings)):
            raise ImproperlyConfigured(
                f"'Settings' in {dotted_path!r} must be a BaseSettings subclass."
            )
        return candidate

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self.load(), name)

    def __repr__(self) -> str:
        state = "configured" if self.configured else "not configured"
        return f"<LazySettings {state}>"


settings = LazySettings()
