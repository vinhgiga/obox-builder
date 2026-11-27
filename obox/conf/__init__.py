"""Lazy uppercase configuration loaded from a settings module."""

from __future__ import annotations

import os
from importlib import import_module
from types import SimpleNamespace
from typing import Any

from obox.conf import global_settings
from obox.core.exceptions import ImproperlyConfigured


class LazySettings:
    def __init__(self) -> None:
        object.__setattr__(self, "_wrapped", None)

    @property
    def configured(self) -> bool:
        return self._wrapped is not None

    def _setup(self) -> None:
        name = os.environ.get("OBOX_SETTINGS_MODULE")
        if not name:
            raise ImproperlyConfigured(
                "Settings are not configured. Set OBOX_SETTINGS_MODULE "
                "or call settings.configure()."
            )
        module = import_module(name)
        self.configure(**{key: getattr(module, key) for key in dir(module) if key.isupper()})
        self._wrapped.SETTINGS_MODULE = name

    def configure(self, default_settings: Any = global_settings, **options: Any) -> None:
        if self.configured:
            raise RuntimeError("Settings already configured.")
        invalid = next((key for key in options if not key.isupper()), None)
        if invalid is not None:
            raise TypeError(f"Setting {invalid!r} must be uppercase.")
        values = {
            key: getattr(default_settings, key) for key in dir(default_settings) if key.isupper()
        }
        object.__setattr__(self, "_wrapped", SimpleNamespace(**{**values, **options}))

    def __getattr__(self, name: str) -> Any:
        if not self.configured:
            self._setup()
        return getattr(self._wrapped, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "_wrapped":
            object.__setattr__(self, name, value)
        else:
            if not self.configured:
                self._setup()
            setattr(self._wrapped, name, value)


settings = LazySettings()
