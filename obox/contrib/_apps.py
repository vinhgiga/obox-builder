"""Shared application setup for optional feature packages."""

from functools import partial
from importlib import import_module
from typing import Any

from obox.apps import AppConfig


class FeatureConfig(AppConfig):
    feature: str
    setting: str

    def get_options(self, settings: Any) -> dict[str, Any]:
        return dict(getattr(settings, self.setting, {}))

    def ready(self) -> None:
        from obox.conf import settings

        feature = getattr(import_module(self.name), self.feature)
        self._service_factory = partial(feature, **self.get_options(settings))
