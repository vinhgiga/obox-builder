"""obox-builder: an internal FastAPI admin framework, Django-like at the mechanism level."""

from __future__ import annotations

from obox_builder.apps import apps
from obox_builder.conf import BaseSettings, LazySettings, settings
from obox_builder.conf.logging import configure_logging
from obox_builder.exceptions import ImproperlyConfigured

__version__ = "0.3.0"

_setup_done = False


def setup() -> BaseSettings:
    """Load settings, configure logging and populate the app registry. Idempotent."""
    global _setup_done
    config = settings.load()
    if _setup_done:
        return config
    configure_logging(logging_config=config.LOGGING, debug=config.DEBUG)
    apps.populate(config.INSTALLED_APPS)
    from obox_builder.resources.registry import initialize

    initialize(app.name for app in apps.get_app_configs())
    for app in apps.get_app_configs():
        app.finalize()
    _setup_done = True
    return config


def configure(config: BaseSettings | type[BaseSettings] | str | None = None) -> None:
    """Configure settings programmatically (scripts and tests)."""
    settings.configure(config)


def _reset() -> None:
    global _setup_done
    _setup_done = False
    apps._reset()
    from obox_builder.resources.registry import reset

    reset()
    import sys

    permissions = sys.modules.get("obox_builder.contrib.auth.permissions")
    if permissions is not None:
        setattr(permissions, "_catalog", None)


__all__ = [
    "BaseSettings",
    "ImproperlyConfigured",
    "LazySettings",
    "__version__",
    "apps",
    "configure",
    "settings",
    "setup",
]
