from dataclasses import replace
from typing import Any

from obox_builder import AppConfig, Settings


def with_builders(settings: Settings, *builders: type[Any]) -> Settings:
    class Resources(AppConfig):
        name = "obox_builder"
        label = "admin"

    Resources.builders = builders
    return replace(settings, installed_apps=(Resources,))
