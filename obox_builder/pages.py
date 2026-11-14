"""Immutable declarations for application pages."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from fastapi import Request

from obox_builder.auth import _valid_permission, has_permission
from obox_builder.context import Context
from obox_builder.errors import ImproperlyConfigured

PageContext = Callable[[Request, Context], Mapping[str, Any] | Awaitable[Mapping[str, Any]]]


@dataclass(frozen=True)
class Page:
    name: str
    path: str | None = field(default=None, kw_only=True)
    template: str | None = field(default=None, kw_only=True)
    title: str | None = field(default=None, kw_only=True)
    context: PageContext | None = field(default=None, kw_only=True)
    permission: str | None = field(default=None, kw_only=True)
    public: bool = field(default=False, kw_only=True)
    menu: bool = field(default=True, kw_only=True)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or re.fullmatch(r"[a-z][a-z0-9_]*", self.name) is None:
            raise ImproperlyConfigured("Page names must be lowercase identifiers.")
        if self.path is not None and (
            not isinstance(self.path, str)
            or not self.path.startswith("/")
            or self.path.endswith("/")
            or any(char in self.path for char in "{}?#\\")
            or any(part in {"", ".", ".."} for part in self.path[1:].split("/"))
        ):
            raise ImproperlyConfigured("Page paths must be static absolute URL paths.")
        if self.template is not None and (
            not isinstance(self.template, str)
            or not self.template.strip()
            or self.template.startswith("/")
            or "\\" in self.template
            or ".." in self.template.split("/")
        ):
            raise ImproperlyConfigured("Page templates require a logical template name.")
        if self.title is not None and (not isinstance(self.title, str) or not self.title.strip()):
            raise ImproperlyConfigured("Page titles must be nonempty strings.")
        if self.context is not None and not callable(self.context):
            raise ImproperlyConfigured("Page context must be a callable.")
        if self.permission is not None and (
            not isinstance(self.permission, str) or not _valid_permission(self.permission)
        ):
            raise ImproperlyConfigured("Page permissions require app.resource.action.")
        if type(self.public) is not bool or type(self.menu) is not bool:
            raise ImproperlyConfigured("Page public and menu options must be booleans.")
        if self.public and self.permission is not None:
            raise ImproperlyConfigured("Public pages must not require a permission.")

    def permitted(self, principal: Any) -> bool:
        return self.public or bool(
            getattr(principal, "is_authenticated", False) is True
            and getattr(principal, "is_active", False) is True
            and (self.permission is None or has_permission(principal, self.permission))
        )

    def route_name(self, app_label: str) -> str:
        return f"obox_page_{app_label}.{self.name}"
