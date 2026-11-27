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
    ListView,
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
    views: bool
    viewset: type[ModelViewSet[Any]] | None = None
    view_suffix: str | None = None
    api_suffix: str | None = None

    def mount(
        self,
        runtime: _Runtime,
        label: str | None = None,
        *,
        view_prefix: str | None = None,
        api_prefix: str | None = None,
    ) -> None:
        runtime.check_mutable()
        if type(self.views) is not bool or (self.api is False and not self.views):
            raise ImproperlyConfigured("Choose at least one ModelResource surface.")
        if self.api is True or self.api is False:
            api_class = ModelApi if self.api else None
        elif isinstance(self.api, type) and issubclass(self.api, ModelApi):
            api_class = self.api
        else:
            raise ImproperlyConfigured("Pass a boolean or a ModelApi subclass to api.")
        if not self.views and self.viewset is not None:
            raise ImproperlyConfigured("Enable views when providing a ModelViewSet.")

        resource = resolve_resource(self.target)
        auth_model = getattr(getattr(runtime.auth, "backend", None), "user_model", None)
        if resource.model is auth_model or resource.model.__module__ == "obox.contrib.auth.models":
            raise ImproperlyConfigured("Authentication models use the dedicated account routes.")
        identity = label, resource.model
        if identity in runtime._resources:
            raise ImproperlyConfigured(f"{resource.model.__name__} is already registered.")
        slug = self.slug if self.slug is not None else _slug(resource.model.__name__)
        if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
            raise ImproperlyConfigured(
                "A ModelResource slug must use lowercase words joined by hyphens."
            )
        view_route = self.view_suffix if self.view_suffix is not None else slug
        api_route = (
            self.api_suffix
            if self.api_suffix is not None
            else self.slug or resource.model.__table__.name
        )
        view_suffix = _route_suffix(view_route, "view")
        api_suffix = _route_suffix(api_route, "API") if api_class is not None else api_route
        app_path = f"/{label}" if label else ""
        view_base = view_prefix or app_path
        api_base = api_prefix or runtime.settings.api_prefix + app_path
        view_path = _join_path(view_base, view_suffix)

        if self.views:
            resource.check_create(resource.writable)
        ui = self._build_views(view_path, resource) if self.views else None
        api_routes = [api_suffix]
        legacy_api_suffix = self.slug or slug
        if api_class is not None and legacy_api_suffix != api_suffix and self.api_suffix is None:
            api_routes.append(_route_suffix(legacy_api_suffix, "API"))
        apis = (
            [
                api_class().build_resource_router(_join_path(api_base, suffix), resource)
                for suffix in api_routes
            ]
            if api_class is not None
            else []
        )
        if ui is not None:
            runtime.add_ui_prefix(view_path)
            runtime.include_router(ui, ui=True)
        for api in apis:
            runtime.include_router(api, app_api=api_prefix is not None)
        if self.views and label:
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

    def _build_views(self, prefix: str, resource: ModelResource[Any]) -> APIRouter:
        basename = f"obox:views:{prefix}"
        router = APIRouter(prefix=prefix, include_in_schema=False, route_class=UIRoute)
        viewset = self.viewset
        primary: type[ListView[Any]] | None = getattr(viewset, "list_view", None)
        additional = getattr(viewset, "collection_views", ())
        if not isinstance(additional, Sequence) or isinstance(additional, str | bytes):
            raise ImproperlyConfigured("Provide a sequence for collection_views.")
        collections: tuple[type[ListView[Any]], ...] = (
            GridView if primary is None else primary,
            *additional,
        )
        collection_routes = []
        slugs: set[str] = set()
        for index, collection in enumerate(collections):
            if not isinstance(collection, type) or not issubclass(collection, ListView):
                raise ImproperlyConfigured("Collection views must be ListView subclasses.")
            slug = collection.slug
            if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
                raise ImproperlyConfigured(
                    "Collection view slugs must be lowercase words joined by hyphens."
                )
            if slug in slugs:
                raise ImproperlyConfigured(f"Collection view slug {slug!r} is already used.")
            slugs.add(slug)
            role = "list" if index == 0 else f"collection:{slug}"
            route = "changelist" if index == 0 else role
            path = "" if index == 0 else f"/views/{slug}"
            collection_routes.append((role, route, path, ListView, collection, "view"))

        routes: tuple[tuple[str, str, str, type[View], type[View] | None, str], ...] = (
            *collection_routes,
            (
                "detail",
                "detail",
                "/{pk}/view",
                DetailView,
                getattr(viewset, "detail_view", None),
                "view",
            ),
            ("create", "add", "/add", CreateView, getattr(viewset, "create_view", None), "add"),
            (
                "update",
                "change",
                "/{pk}/change",
                UpdateView,
                getattr(viewset, "update_view", None),
                "change",
            ),
            (
                "delete",
                "delete",
                "/{pk}/delete",
                DeleteView,
                getattr(viewset, "delete_view", None),
                "delete",
            ),
        )
        route_names = {role: f"{basename}:{name}" for role, name, *_ in routes}
        collection_links = tuple(
            (view.slug, f"{view.slug.replace('-', ' ').capitalize()} view", route_names[role])
            for view, (role, *_) in zip(collections, collection_routes, strict=True)
        )
        for role, _, path, base, custom, permission in routes:
            if custom is not None and (
                not isinstance(custom, type) or not issubclass(custom, base)
            ):
                raise ImproperlyConfigured(f"Provide a {base.__name__} subclass for {role}_view.")
            view = base if custom is None else custom
            options: dict[str, Any] = {"resource": resource, "route_names": route_names}
            if issubclass(view, ListView):
                options["collection_routes"] = collection_links
                if role == "list" and view is GridView:
                    options["actions"] = ("delete_selected",)
            view.as_route(
                router,
                path,
                name=route_names[role],
                initkwargs=options,
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
    registrations = []
    for resource in dict.fromkeys([*views, *apis]):
        view, api = views.get(resource), apis.get(resource)
        view_slug = view.slug if view is not None else None
        api_slug = api.slug if api is not None else None
        if view_slug is not None and api_slug is not None and view_slug != api_slug:
            raise ImproperlyConfigured(
                f"Use the same slug for {resource.__name__} in {app.label}.views and .apis."
            )
        view_suffix = _declared_suffix(view) if view is not None else None
        api_suffix = _declared_suffix(api) if api is not None else None
        if view_suffix is None:
            view_suffix = api_suffix
        if api_suffix is None:
            api_suffix = view_suffix
        registrations.append(
            Registration(
                target=resource,
                slug=api_slug if view_slug is None else view_slug,
                api=api if api is not None else False,
                views=view is not None,
                viewset=view,
                view_suffix=view_suffix,
                api_suffix=api_suffix,
            )
        )
    return registrations


def _declared_suffix(adapter: type[Any]) -> str | None:
    prefix = getattr(adapter, "prefix", None)
    return prefix if prefix is not None else getattr(adapter, "slug", None)


def _route_suffix(value: str, surface: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"/?[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*", value
    ):
        raise ImproperlyConfigured(f"A {surface} route prefix must be an absolute URL path suffix.")
    return value.lstrip("/")


def _join_path(base: str, suffix: str) -> str:
    return f"{base.rstrip('/')}/{suffix}" if base else f"/{suffix}"


def _slug(name: str) -> str:
    words = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1-\2", name)
    words = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", words).replace("_", "-").lower()
    if words.endswith(("s", "x", "z", "ch", "sh")):
        return words + "es"
    if re.search(r"[^aeiou]y$", words):
        return words[:-1] + "ies"
    return words + "s"
