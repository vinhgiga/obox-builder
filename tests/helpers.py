from dataclasses import replace
from typing import Any

from obox._catalog import CatalogConfig
from obox._config import RuntimeConfig


def with_builders(settings: RuntimeConfig, *builders: type[Any]) -> RuntimeConfig:

    class Resources(CatalogConfig):
        name = "obox"
        label = "admin"

    Resources.builders = builders
    return replace(settings, installed_apps=(Resources,))
