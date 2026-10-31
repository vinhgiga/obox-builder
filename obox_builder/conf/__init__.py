"""Configuration and project scaffolding resources."""

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from importlib import import_module
from os import PathLike
from typing import Any

from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from obox_builder.auth import Policy
from obox_builder.errors import ImproperlyConfigured


@dataclass(frozen=True)
class Settings:
    session_factory: async_sessionmaker[AsyncSession] = field(repr=False)
    principal_dependency: Callable[..., Any] | None = field(default=None, repr=False)
    default_policy: Policy[Any] | None = field(default=None, repr=False)
    ui_prefix: str = "/admin"
    api_prefix: str = "/api"
    csrf_secret: str | bytes | None = field(default=None, repr=False)
    template_dirs: Sequence[str | PathLike[str]] = ()
    templates: Jinja2Templates | None = field(default=None, repr=False)
    base_template: str = "obox/base.html"

    def __post_init__(self) -> None:
        for name in ("ui_prefix", "api_prefix"):
            prefix = getattr(self, name).rstrip("/")
            if not prefix.startswith("/") or any(char in prefix for char in "{}?#"):
                raise ImproperlyConfigured("Surface prefixes must be nonempty absolute URL paths.")
            object.__setattr__(self, name, prefix)
        if self.templates is not None and self.template_dirs:
            raise ImproperlyConfigured("Pass either templates or template_dirs, not both.")


def load_settings(settings: Settings | str | None) -> Settings:
    if isinstance(settings, Settings):
        return settings
    module_name = settings if settings is not None else os.environ.get("OBOX_SETTINGS_MODULE")
    if not module_name:
        raise ImproperlyConfigured("Pass settings or set OBOX_SETTINGS_MODULE.")
    configured = getattr(import_module(module_name), "OBOX", None)
    if not isinstance(configured, Settings):
        raise ImproperlyConfigured(f"{module_name} must define OBOX as a Settings instance.")
    return configured
