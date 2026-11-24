"""Model identities used by permissions, templates and routes."""

from typing import Any


def model_label(model: type[Any]) -> tuple[str, str]:
    from obox.apps import apps

    candidates = [
        config
        for config in apps.app_configs.values()
        if model.__module__ == config.name or model.__module__.startswith(config.name + ".")
    ]
    label = (
        max(candidates, key=lambda config: len(config.name)).label
        if candidates
        else model.__module__.split(".")[-2]
        if "." in model.__module__
        else "app"
    )
    return label, model.__name__.lower()
