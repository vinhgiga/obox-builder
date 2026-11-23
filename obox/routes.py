"""Immutable declarations for HTML routes backed by templates."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from obox._auth import _valid_permission
from obox.core.exceptions import ImproperlyConfigured

if TYPE_CHECKING:
    from obox.views.generic import TemplateView


@dataclass(frozen=True)
class Route:
    path: str
    template: str
    name: str | None = None
    title: str | None = None
    permission: str | None = None
    menu: bool = True
    view: type[TemplateView] | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.path, str)
            or not self.path.startswith("/")
            or self.path.endswith("/")
            or any(char in self.path for char in "{}?#\\")
            or any(part in {"", ".", ".."} for part in self.path[1:].split("/"))
        ):
            raise ImproperlyConfigured("Route paths must be static absolute URL paths.")
        if (
            not isinstance(self.template, str)
            or not self.template.strip()
            or self.template.startswith("/")
            or "\\" in self.template
            or ".." in self.template.split("/")
        ):
            raise ImproperlyConfigured("Routes require a logical template name.")
        name = self.name or self.path.rsplit("/", 1)[-1].replace("-", "_")
        if not isinstance(name, str) or re.fullmatch(r"[a-z][a-z0-9_]*", name) is None:
            raise ImproperlyConfigured("Route names must be lowercase identifiers.")
        object.__setattr__(self, "name", name)
        if self.title is not None and (not isinstance(self.title, str) or not self.title.strip()):
            raise ImproperlyConfigured("Route titles must be nonempty strings.")
        if self.permission is not None and (
            not isinstance(self.permission, str)
            or not (
                _valid_permission(self.permission)
                or (self.permission.isidentifier() and not self.permission.startswith("_"))
            )
        ):
            raise ImproperlyConfigured("Route permissions require a name or app.resource.action.")
        if type(self.menu) is not bool:
            raise ImproperlyConfigured("Route menu must be a boolean.")
        if self.view is not None:
            from obox.views.generic import TemplateView

            if not isinstance(self.view, type) or not issubclass(self.view, TemplateView):
                raise ImproperlyConfigured("Route views must inherit TemplateView.")

    def route_name(self, app_label: str) -> str:
        return f"obox_route_{app_label}.{self.name}"
