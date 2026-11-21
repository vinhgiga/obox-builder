"""Bind a resource to the selected FastAPI surfaces."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from obox.builder.options import ModelBuilder
from obox.builder.routes import build_views
from obox.core.exceptions import ImproperlyConfigured
from obox.resources import resolve_resource

if TYPE_CHECKING:
    from obox._runtime import _Runtime


@dataclass(frozen=True)
class Registration:
    target: type[Any]
    builder: type[ModelBuilder] | None
    api: bool
    views: bool

    def mount(self, runtime: _Runtime, label: str | None = None) -> None:
        runtime._check_mutable()
        if (
            type(self.api) is not bool
            or type(self.views) is not bool
            or not (self.api or self.views)
        ):
            raise ImproperlyConfigured("Choose at least one Resource surface.")
        resource = resolve_resource(self.target)
        builder = self.builder or ModelBuilder
        if not isinstance(builder, type) or not issubclass(builder, ModelBuilder):
            raise ImproperlyConfigured("Provide a ModelBuilder subclass.")
        auth_model = getattr(getattr(runtime.auth, "backend", None), "user_model", None)
        if resource.model is auth_model or resource.model.__module__ == "obox.contrib.auth.models":
            raise ImproperlyConfigured("Authentication models use the dedicated account routes.")
        identity = label, resource.model
        if identity in runtime._resources:
            raise ImproperlyConfigured(f"{resource.model.__name__} is already registered.")
        options = builder(resource.model, resource)
        slug = options.slug if options.slug is not None else _slug(resource.model.__name__)
        if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
            raise ImproperlyConfigured(
                "A Resource slug must use lowercase words joined by hyphens."
            )
        prefix = f"/{label}/{slug}" if label else f"/{slug}"

        if self.views:
            resource._check_create(resource.writable)
        ui = build_views(prefix, resource, options) if self.views else None
        api = (
            options.api_class()._build_router(runtime.settings.api_prefix + prefix, resource)
            if self.api
            else None
        )
        if ui is not None:
            runtime.add_ui_prefix(prefix)
            runtime.include_router(ui, ui=True)
        if api is not None:
            runtime.include_router(api)
        runtime._resources[identity] = resource.model


def _slug(name: str) -> str:
    words = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1-\2", name)
    words = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", words).replace("_", "-").lower()
    if words.endswith(("s", "x", "z", "ch", "sh")):
        return words + "es"
    if re.search(r"[^aeiou]y$", words):
        return words[:-1] + "ies"
    return words + "s"
