from importlib import import_module
from importlib.util import find_spec

from obox.apps import AppConfig


class BuilderConfig(AppConfig):
    name = "obox.builder"
    verbose_name = "Administration"

    def ready(self) -> None:
        for config in self.apps.get_app_configs():
            if hasattr(config.module, "__path__") and find_spec(config.name + ".admin") is not None:
                import_module(config.name + ".admin")
