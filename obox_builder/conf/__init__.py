"""Configuration and project scaffolding resources."""

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, fields
from importlib import import_module
from os import PathLike
from typing import Any

from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from obox_builder.auth import Policy
from obox_builder.errors import ImproperlyConfigured


@dataclass(frozen=True)
class Settings:
    session_factory: async_sessionmaker[AsyncSession] | None = field(default=None, repr=False)
    database_url: str | None = field(default=None, repr=False)
    principal_dependency: Callable[..., Any] | None = field(default=None, repr=False)
    default_policy: Policy[Any] | None = field(default=None, repr=False)
    ui_prefix: str = "/admin"
    api_prefix: str = "/api"
    csrf_secret: str | bytes | None = field(default=None, repr=False)
    template_dirs: Sequence[str | PathLike[str]] = ()
    templates: Jinja2Templates | None = field(default=None, repr=False)
    base_template: str = "obox/base.html"

    def __post_init__(self) -> None:
        if (self.database_url is None) == (self.session_factory is None):
            raise ImproperlyConfigured("Provide exactly one of DATABASE_URL or SESSION_FACTORY.")
        if self.database_url is not None and (
            not isinstance(self.database_url, str) or not self.database_url.strip()
        ):
            raise ImproperlyConfigured("DATABASE_URL must be a nonempty string.")
        if self.session_factory is not None and not isinstance(
            self.session_factory, async_sessionmaker
        ):
            raise ImproperlyConfigured("SESSION_FACTORY must be an async_sessionmaker.")
        if self.principal_dependency is not None and not callable(self.principal_dependency):
            raise ImproperlyConfigured("PRINCIPAL_DEPENDENCY must be callable.")
        if self.default_policy is not None and not isinstance(self.default_policy, Policy):
            raise ImproperlyConfigured("DEFAULT_POLICY must be a Policy instance.")
        if self.csrf_secret is not None and (
            not isinstance(self.csrf_secret, str | bytes)
            or len(
                self.csrf_secret.encode() if isinstance(self.csrf_secret, str) else self.csrf_secret
            )
            < 32
        ):
            raise ImproperlyConfigured("SECRET_KEY must contain at least 32 bytes.")
        if not isinstance(self.template_dirs, Sequence) or isinstance(
            self.template_dirs, str | bytes
        ):
            raise ImproperlyConfigured("TEMPLATE_DIRS must be a sequence of paths.")
        if any(not isinstance(path, str | PathLike) for path in self.template_dirs):
            raise ImproperlyConfigured("TEMPLATE_DIRS must contain paths.")
        if self.templates is not None and not isinstance(self.templates, Jinja2Templates):
            raise ImproperlyConfigured("TEMPLATES must be a Jinja2Templates instance.")
        if not isinstance(self.base_template, str) or not self.base_template:
            raise ImproperlyConfigured("BASE_TEMPLATE must be a nonempty string.")
        for name in ("ui_prefix", "api_prefix"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise ImproperlyConfigured("Surface prefixes must be strings.")
            prefix = value.rstrip("/")
            if not prefix.startswith("/") or any(char in prefix for char in "{}?#"):
                raise ImproperlyConfigured("Surface prefixes must be nonempty absolute URL paths.")
            object.__setattr__(self, name, prefix)
        if self.templates is not None and self.template_dirs:
            raise ImproperlyConfigured("Pass either templates or template_dirs, not both.")
        object.__setattr__(self, "template_dirs", tuple(self.template_dirs))


def load_settings(settings: Settings | str | None = None) -> Settings:
    if isinstance(settings, Settings):
        return settings
    module_name = settings or os.environ.get("OBOX_SETTINGS_MODULE", "setting")
    module = import_module(module_name)
    values = {}
    for item in fields(Settings):
        name = "SECRET_KEY" if item.name == "csrf_secret" else item.name.upper()
        if hasattr(module, name):
            values[item.name] = getattr(module, name)
    return Settings(**values)
