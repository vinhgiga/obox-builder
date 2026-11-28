"""Bind a resource to the selected FastAPI surfaces."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import import_module
from typing import TYPE_CHECKING, Any, TypeVar

from fastapi import APIRouter, Depends

from obox.api import ModelApi
from obox.core.exceptions import ImproperlyConfigured
from obox.resources import ModelResource, resolve_resource
from obox.views.generic import (
    CreateView,
    DeleteView,
    DetailView,
    GridView,
    ModelViewSet,
    UpdateView,
    View,
)
from obox.views.generic._model import model_permission
from obox.web.dependencies import permission_required
from obox.web.routes import UIRoute

if TYPE_CHECKING:
    from obox.apps import AppConfig
    from obox.runtime.runtime import _Runtime

AdapterT = TypeVar("AdapterT", bound=ModelViewSet[Any] | ModelApi[Any])


@dataclass(frozen=True)
class Registration:
    target: type[Any]
    slug: str | None
    api: bool | type[ModelApi[Any]]
    views: bool | type[ModelViewSet[Any]]

    def mount(
        self,
        runtime: _Runtime,
        label: str | None = None,
        *,
        view_prefix: str | None = None,
        api_prefix: str | None = None,
    ) -> None:
        runtime.check_mutable()
        api_class = _surface(self.api, ModelApi)
        viewset = _surface(self.views, ModelViewSet)
        if api_class is None and viewset is None:
            raise ImproperlyConfigured("Choose at least one ModelResource surface.")

        resource = resolve_resource(self.target)
        auth_model = getattr(getattr(runtime.auth, "backend", None), "user_model", None)
        if resource.model is auth_model or resource.model.__module__ == "obox.contrib.auth.models":
            raise ImproperlyConfigured("Authentication models use the dedicated account routes.")
        identity = label, resource.model
        if identity in runtime._resources:
            raise ImproperlyConfigured(f"{resource.model.__name__} is already registered.")
        view_slug = getattr(viewset, "slug", None)
        api_slug = getattr(api_class, "slug", None)
        if view_slug is not None and api_slug is not None and view_slug != api_slug:
            raise ImproperlyConfigured(
                f"Use the same slug for {resource.model.__name__} in {label}.views and .apis."
            )
        declared_slug = self.slug if self.slug is not None else view_slug or api_slug
        slug = (
            declared_slug if declared_slug is not None else _default_slug(resource.model.__name__)
        )
        if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
            raise ImproperlyConfigured(
                "A ModelResource slug must use lowercase words joined by hyphens."
            )
        view_route = getattr(viewset, "prefix", None)
        api_route = getattr(api_class, "prefix", None)
        view_suffix = _route_suffix(view_route or api_route or slug, "view")
        api_suffix = (
            _route_suffix(
                api_route or view_route or declared_slug or resource.model.__table__.name, "API"
            )
            if api_class is not None
            else ""
        )
        app_path = f"/{label}" if label else ""
        view_base = view_prefix or app_path
        api_base = api_prefix or runtime.settings.api_prefix + app_path
        view_path = _join_path(view_base, view_suffix)

        if viewset is not None:
            resource.check_create(resource.writable)
        ui = self._build_views(view_path, resource, viewset) if viewset is not None else None
        api_router = (
            api_class().build_resource_router(_join_path(api_base, api_suffix), resource)
            if api_class is not None
            else None
        )
        if ui is not None:
            runtime.add_ui_prefix(view_path)
            runtime.include_router(ui, ui=True)
        if api_router is not None:
            runtime.include_router(api_router, app_api=api_prefix is not None)
        if viewset is not None and label:
            catalog = runtime._apps.get(label)
            if catalog is not None:
                catalog._registered_resources.append(
                    {
                        "label": resource.model.__name__,
                        "url": view_path,
                        "view_permission": model_permission(resource.model, "view"),
                        "add_permission": model_permission(resource.model, "add"),
                    }
                )
        runtime._resources[identity] = resource.model

    @staticmethod
    def _build_views(
        prefix: str, resource: ModelResource[Any], viewset: type[ModelViewSet[Any]]
    ) -> APIRouter:
        basename = f"obox:views:{prefix}"
        router = APIRouter(prefix=prefix, include_in_schema=False, route_class=UIRoute)
        routes: tuple[tuple[str, str, str, type[View], str], ...] = (
            ("list", "changelist", "", GridView, "view"),
            ("detail", "detail", "/{pk}/view", DetailView, "view"),
            ("create", "add", "/add", CreateView, "add"),
            ("update", "change", "/{pk}/change", UpdateView, "change"),
            ("delete", "delete", "/{pk}/delete", DeleteView, "delete"),
        )
        route_names = {role: f"{basename}:{name}" for role, name, *_ in routes}
        for role, _, path, base, permission in routes:
            custom = getattr(viewset, f"{role}_view")
            if custom is not None and (
                not isinstance(custom, type) or not issubclass(custom, base)
            ):
                raise ImproperlyConfigured(f"Provide a {base.__name__} subclass for {role}_view.")
            view = base if custom is None else custom
            view.as_route(
                router,
                path,
                name=route_names[role],
                initkwargs={"resource": resource, "route_names": route_names},
                dependencies=[
                    Depends(
                        permission_required(
                            model_permission(resource.model, permission),
                            html=True,
                        )
                    )
                ],
            )
        return router


def _app_bindings(
    app: AppConfig, field: str, adapter: type[AdapterT]
) -> dict[type[ModelResource[Any]], type[AdapterT]]:
    declarations = getattr(app, field)
    setting = f"{app.label}.{field}"
    if not isinstance(declarations, Sequence) or isinstance(declarations, (str, bytes)):
        raise ImproperlyConfigured(f"{setting} must be a list or tuple of declarations.")
    bindings: dict[type[ModelResource[Any]], type[AdapterT]] = {}
    for declaration in declarations:
        if isinstance(declaration, str):
            module, _, name = declaration.rpartition(".")
            try:
                declaration = getattr(import_module(module), name)
            except (ImportError, AttributeError, ValueError) as exc:
                raise ImproperlyConfigured(f"Cannot load {declaration!r} in {setting}.") from exc
        if isinstance(declaration, type) and issubclass(declaration, ModelResource):
            resource, binding = declaration, adapter
        elif isinstance(declaration, type) and issubclass(declaration, adapter):
            target = getattr(declaration, "resource", None)
            if (
                not isinstance(target, type)
                or not issubclass(target, ModelResource)
                or getattr(declaration, "model", None) is not None
            ):
                raise ImproperlyConfigured(
                    f"{declaration.__name__} in {setting} must declare a ModelResource class."
                )
            resource, binding = target, declaration
        else:
            raise ImproperlyConfigured(
                f"{setting} must contain ModelResource or {adapter.__name__} classes."
            )
        if resource in bindings:
            raise ImproperlyConfigured(f"{resource.__name__} is declared twice in {setting}.")
        bindings[resource] = binding
    return bindings


def app_registrations(app: AppConfig) -> list[Registration]:
    views = _app_bindings(app, "views", ModelViewSet)
    apis = _app_bindings(app, "apis", ModelApi)
    return [
        Registration(resource, None, apis.get(resource, False), views.get(resource, False))
        for resource in dict.fromkeys([*views, *apis])
    ]


def _surface(value: bool | type[AdapterT], adapter: type[AdapterT]) -> type[AdapterT] | None:
    if value is True:
        return adapter
    if value is False:
        return None
    if isinstance(value, type) and issubclass(value, adapter):
        return value
    raise ImproperlyConfigured(f"Pass a boolean or a {adapter.__name__} subclass.")


def _route_suffix(value: str, surface: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"/?[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*", value
    ):
        raise ImproperlyConfigured(f"A {surface} route prefix must be an absolute URL path suffix.")
    return value.lstrip("/")


def _join_path(base: str, suffix: str) -> str:
    return f"{base.rstrip('/')}/{suffix}" if base else f"/{suffix}"


def _default_slug(name: str) -> str:
    words = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1-\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", words).replace("_", "-").lower()
