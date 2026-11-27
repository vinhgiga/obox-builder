"""Configuration and project scaffolding resources."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from os import PathLike
from types import MappingProxyType
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from obox.core.exceptions import ImproperlyConfigured


@dataclass(frozen=True)
class RuntimeConfig:
    session_factory: async_sessionmaker[AsyncSession] = field(repr=False)
    principal_dependency: Callable[..., Any] | None = field(default=None, repr=False)
    ui_prefix: str = "/admin"
    api_prefix: str = "/v0"
    csrf_secret: str | bytes | None = field(default=None, repr=False)
    template_dirs: Sequence[str | PathLike[str]] = ()
    base_template: str = "obox/base.html"
    static_root: str | PathLike[str] | None = None
    auth_password_validators: Sequence[Mapping[str, Any]] = ()
    login_redirect_url: str | None = None

    @classmethod
    def from_settings(cls) -> RuntimeConfig:
        from obox.conf import settings
        from obox.db.engine import default_session_factory

        template = settings.TEMPLATES[0] if settings.TEMPLATES else {}
        return cls(
            session_factory=default_session_factory(),
            ui_prefix="",
            api_prefix=settings.API_PREFIX,
            csrf_secret=settings.SECRET_KEY or None,
            template_dirs=template.get("DIRS", ()),
            static_root=settings.STATIC_ROOT,
            auth_password_validators=settings.AUTH_PASSWORD_VALIDATORS,
            login_redirect_url=settings.LOGIN_REDIRECT_URL,
        )

    def __post_init__(self) -> None:
        if not isinstance(self.session_factory, async_sessionmaker):
            raise ImproperlyConfigured("Provide an async session factory.")
        if self.csrf_secret is not None and (
            not isinstance(self.csrf_secret, str | bytes)
            or len(
                self.csrf_secret.encode() if isinstance(self.csrf_secret, str) else self.csrf_secret
            )
            < 32
        ):
            raise ImproperlyConfigured("SECRET_KEY must contain at least 32 bytes.")
        if isinstance(self.template_dirs, str | bytes):
            raise ImproperlyConfigured("TEMPLATE_DIRS must be a sequence of paths.")
        if any(not isinstance(path, str | PathLike) for path in self.template_dirs):
            raise ImproperlyConfigured("TEMPLATE_DIRS must contain paths.")
        if not self.base_template:
            raise ImproperlyConfigured("BASE_TEMPLATE must be a nonempty string.")
        if self.login_redirect_url is not None and (
            not isinstance(self.login_redirect_url, str) or not self.login_redirect_url
        ):
            raise ImproperlyConfigured("LOGIN_REDIRECT_URL must be a nonempty string.")
        if self.static_root is not None and not os.fspath(self.static_root):
            raise ImproperlyConfigured("STATIC_ROOT must be a nonempty path or None.")
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
        object.__setattr__(self, "template_dirs", tuple(self.template_dirs))
        object.__setattr__(self, "auth_password_validators", tuple(validators))
