"""Bind a resource to the selected FastAPI surfaces."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import import_module
from typing import TYPE_CHECKING, Any, TypeVar, cast

from fastapi import APIRouter, Depends

from obox._dependencies import permission_required
from obox._page_routes import PageRoute
from obox.api import ModelApi
from obox.core.exceptions import ImproperlyConfigured
from obox.resources import Resource, resolve_resource
from obox.views.generic import (
    CreateView,
    DeleteView,
    DetailView,
    ListView,
    ModelViewSet,
    UpdateView,
)
from obox.views.generic._model import model_permission

if TYPE_CHECKING:
    from obox._runtime import _Runtime
    from obox.apps import AppConfig

AdapterT = TypeVar("AdapterT", bound=ModelViewSet[Any] | ModelApi[Any])


@dataclass(frozen=True)
class Registration:
    target: type[Any]
    slug: str | None
    api: bool | type[ModelApi[Any]]
    views: bool
    list_view: type[ListView] | None
    detail_view: type[DetailView] | None
    create_view: type[CreateView] | None
    update_view: type[UpdateView] | None
    delete_view: type[DeleteView] | None

    def mount(
        self,
        runtime: _Runtime,
        label: str | None = None,
        *,
        view_prefix: str | None = None,
        api_prefix: str | None = None,
    ) -> None:
        runtime._check_mutable()
        if type(self.views) is not bool or (self.api is False and not self.views):
            raise ImproperlyConfigured("Choose at least one Resource surface.")
        if self.api is True or self.api is False:
            api_class = ModelApi if self.api else None
        elif isinstance(self.api, type) and issubclass(self.api, ModelApi):
            api_class = self.api
        else:
            raise ImproperlyConfigured("Pass a boolean or a ModelApi subclass to api.")
        if not self.views and any(
            view is not None
            for view in (
                self.list_view,
                self.detail_view,
                self.create_view,
                self.update_view,
                self.delete_view,
            )
        ):
            raise ImproperlyConfigured("Enable views when providing custom view classes.")

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
                "A Resource slug must use lowercase words joined by hyphens."
            )
        app_path = f"/{label}" if label else ""
        view_path = f"{view_prefix or app_path}/{slug}"
        api_path = f"{api_prefix or runtime.settings.api_prefix + app_path}/{slug}"

        if self.views:
            resource._check_create(resource.writable)
        ui = self._build_views(view_path, resource) if self.views else None
        api = api_class()._build_router(api_path, resource) if api_class is not None else None
        if ui is not None:
            runtime.add_ui_prefix(view_path)
            runtime.include_router(ui, ui=True)
        if api is not None:
            runtime.include_router(api, app_api=api_prefix is not None)
        runtime._resources[identity] = resource.model

    def _build_views(self, prefix: str, resource: Resource[Any]) -> APIRouter:
        basename = f"obox:views:{prefix}"
        router = APIRouter(prefix=prefix, include_in_schema=False, route_class=PageRoute)
        routes = (
            ("list", "changelist", "", ListView, self.list_view, "view"),
            ("detail", "detail", "/{pk}/view", DetailView, self.detail_view, "view"),
            ("create", "add", "/add", CreateView, self.create_view, "add"),
            ("update", "change", "/{pk}/change", UpdateView, self.update_view, "change"),
            ("delete", "delete", "/{pk}/delete", DeleteView, self.delete_view, "delete"),
        )
        route_names = {role: f"{basename}:{name}" for role, name, *_ in routes}
        for role, _, path, base, custom, permission in routes:
            if custom is not None and (
                not isinstance(custom, type) or not issubclass(custom, base)
            ):
                raise ImproperlyConfigured(f"Provide a {base.__name__} subclass for {role}_view.")
            view = base if custom is None else custom
            options: dict[str, Any] = {"resource": resource, "route_names": route_names}
            if issubclass(view, ListView):
                list_view = cast("type[ListView]", view)
                options.update(
                    paginate_by=list_view.paginate_by if list_view.paginate_by is not None else 100,
                    page_kwarg="p",
                    page_start=0,
                    actions=(
                        None
                        if list_view.actions is None
                        else ("delete_selected", *list_view.actions)
                    ),
                )
            methods = ["GET", "HEAD"] if role == "detail" else ["GET", "HEAD", "POST"]
            router.add_api_route(
                path,
                view.as_view(**options),
                methods=methods,
                name=route_names[role],
                dependencies=[
                    Depends(
                        permission_required(model_permission(resource.model, permission), html=True)
                    )
                ],
                response_model=None,
                include_in_schema=False,
            )
        return router


def _app_bindings(
    app: AppConfig, field: str, adapter: type[AdapterT]
) -> dict[type[Resource[Any]], type[AdapterT]]:
    declarations = getattr(app, field)
    setting = f"{app.label}.{field}"
    if not isinstance(declarations, Sequence) or isinstance(declarations, (str, bytes)):
        raise ImproperlyConfigured(f"{setting} must be a list or tuple of declarations.")
    bindings: dict[type[Resource[Any]], type[AdapterT]] = {}
    for declaration in declarations:
        if isinstance(declaration, str):
            module, _, name = declaration.rpartition(".")
            try:
                declaration = getattr(import_module(module), name)
            except (ImportError, AttributeError, ValueError) as exc:
                raise ImproperlyConfigured(f"Cannot load {declaration!r} in {setting}.") from exc
        if isinstance(declaration, type) and issubclass(declaration, Resource):
            resource, binding = declaration, adapter
        elif isinstance(declaration, type) and issubclass(declaration, adapter):
            target = getattr(declaration, "resource", None)
            if (
                not isinstance(target, type)
                or not issubclass(target, Resource)
                or getattr(declaration, "model", None) is not None
            ):
                raise ImproperlyConfigured(
                    f"{declaration.__name__} in {setting} must declare a Resource class."
                )
            resource, binding = target, declaration
        else:
            raise ImproperlyConfigured(
                f"{setting} must contain Resource or {adapter.__name__} classes."
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
        registrations.append(
            Registration(
                target=resource,
                slug=api_slug if view_slug is None else view_slug,
                api=api if api is not None else False,
                views=view is not None,
                list_view=view.list_view if view is not None else None,
                detail_view=view.detail_view if view is not None else None,
                create_view=view.create_view if view is not None else None,
                update_view=view.update_view if view is not None else None,
                delete_view=view.delete_view if view is not None else None,
            )
        )
    return registrations


def _slug(name: str) -> str:
    words = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1-\2", name)
    words = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", words).replace("_", "-").lower()
    if words.endswith(("s", "x", "z", "ch", "sh")):
        return words + "es"
    if re.search(r"[^aeiou]y$", words):
        return words[:-1] + "ies"
    return words + "s"
