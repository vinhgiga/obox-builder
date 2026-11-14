"""Configuration and project scaffolding resources."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, fields
from importlib import import_module
from os import PathLike
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from obox_builder.auth import Policy, StaffOnly
from obox_builder.errors import ImproperlyConfigured

if TYPE_CHECKING:
    from obox_builder.apps import AppConfig


@dataclass(frozen=True)
class Settings:
    session_factory: async_sessionmaker[AsyncSession] | None = field(default=None, repr=False)
    database_url: str | None = field(default=None, repr=False)
    principal_dependency: Callable[..., Any] | None = field(default=None, repr=False)
    default_policy: Policy[Any] | None = field(default=None, repr=False)
    ui_prefix: str = "/admin"
    api_prefix: str = "/api"
    api_default_limit: int | None = None
    api_max_limit: int | None = None
    api_max_batch: int | None = None
    csrf_secret: str | bytes | None = field(default=None, repr=False)
    template_dirs: Sequence[str | PathLike[str]] = ()
    templates: Jinja2Templates | None = field(default=None, repr=False)
    base_template: str = "obox/base.html"
    static_root: str | PathLike[str] | None = None
    auth_password_validators: Sequence[Mapping[str, Any]] = ()
    login_redirect_url: str = "/accounts/profile/"
    installed_apps: Sequence[str | type[AppConfig] | AppConfig] | None = None

    def __post_init__(self) -> None:
        for value in (self.api_default_limit, self.api_max_limit, self.api_max_batch):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ImproperlyConfigured("API limits must be nonnegative integers or None.")
        if (
            self.api_default_limit is not None
            and self.api_max_limit is not None
            and self.api_default_limit > self.api_max_limit
        ):
            raise ImproperlyConfigured("API_DEFAULT_LIMIT must not exceed API_MAX_LIMIT.")
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
        if isinstance(self.default_policy, StaffOnly):
            raise ImproperlyConfigured("StaffOnly is no longer supported; use RBAC instead.")
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
        if not isinstance(self.login_redirect_url, str) or not self.login_redirect_url:
            raise ImproperlyConfigured("LOGIN_REDIRECT_URL must be a nonempty string.")
        if self.static_root is not None and (
            not isinstance(self.static_root, str | PathLike) or not os.fspath(self.static_root)
        ):
            raise ImproperlyConfigured("STATIC_ROOT must be a nonempty path or None.")
        if not isinstance(self.auth_password_validators, Sequence):
            raise ImproperlyConfigured("AUTH_PASSWORD_VALIDATORS must be a sequence.")
        validators: list[Mapping[str, Any]] = []
        for validator in self.auth_password_validators:
            if not isinstance(validator, Mapping) or not isinstance(validator.get("NAME"), str):
                raise ImproperlyConfigured(
                    "Each AUTH_PASSWORD_VALIDATORS entry must have a string NAME."
                )
            options = validator.get("OPTIONS", {})
            if not isinstance(options, Mapping):
                raise ImproperlyConfigured("Validator OPTIONS must be a mapping.")
            validators.append(
                MappingProxyType(
                    {"NAME": validator["NAME"], "OPTIONS": MappingProxyType(dict(options))}
                )
            )
        for name in ("ui_prefix", "api_prefix"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise ImproperlyConfigured("Surface prefixes must be strings.")
            prefix = value.rstrip("/")
            if (
                (not prefix and name != "ui_prefix")
                or (prefix and not prefix.startswith("/"))
                or any(char in prefix for char in "{}?#")
            ):
                raise ImproperlyConfigured(
                    "Use absolute surface paths; only UI_PREFIX may be empty."
                )
            object.__setattr__(self, name, prefix)
        if self.templates is not None and self.template_dirs:
            raise ImproperlyConfigured("Pass either templates or template_dirs, not both.")
        object.__setattr__(self, "template_dirs", tuple(self.template_dirs))
        object.__setattr__(self, "auth_password_validators", tuple(validators))
        if self.installed_apps is not None:
            if isinstance(self.installed_apps, (str, bytes)) or not isinstance(
                self.installed_apps, Sequence
            ):
                raise ImproperlyConfigured(
                    "INSTALLED_APPS must be a sequence of app configurations."
                )
            object.__setattr__(self, "installed_apps", tuple(self.installed_apps))


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
