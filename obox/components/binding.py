"""Bind declared template routes and their component services."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastapi import Request

from obox.routes import Route

if TYPE_CHECKING:
    from obox.runtime.runtime import _Runtime


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str


def bind_templates(self: Any) -> None:
    import hashlib
    import json
    from collections.abc import Mapping
    from dataclasses import replace
    from importlib import import_module
    from typing import cast

    from fastapi import APIRouter, Depends
    from fastapi.routing import iter_route_contexts

    from obox.components.declarations import Component, component_name
    from obox.components.scan import ComponentUse, scan_template
    from obox.components.service import ComponentService
    from obox.components.ui import (
        build_component_ui,
        install_component_global,
        resolve_components,
        use_component_request,
    )
    from obox.core.async_utils import invoke
    from obox.core.exceptions import ImproperlyConfigured
    from obox.views.generic import TemplateView
    from obox.views.generic.base import HTMLRoute
    from obox.web.dependencies import permission_required
    from obox.web.i18n import translate

    class RouteTemplateMixin:
        _obox_runtime: _Runtime
        _obox_app_label: str
        _obox_route: Route
        request: Request

        async def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
            values = await invoke(cast(Any, super()).get_context_data, **kwargs)
            if not isinstance(values, Mapping):
                raise ImproperlyConfigured("TemplateView context must return a mapping.")
            request = self.request
            context = dict(values)
            context["title"] = translate(
                request,
                self._obox_route.title
                or cast(str, self._obox_route.name).replace("_", " ").title(),
            )
            context.update(
                await resolve_components(
                    self._obox_runtime,
                    self._obox_app_label,
                    cast(str, self._obox_route.name),
                    request,
                    request.state.obox_context,
                )
            )
            return context

        def get_template_names(self) -> list[str]:
            return [self._obox_route.template]

        def render_to_response(self, context: dict[str, Any], **kwargs: Any) -> Any:
            with use_component_request(self.request):
                return cast(Any, super()).render_to_response(context, **kwargs)

    self._route_diagnostics.clear()
    service_signatures: dict[tuple[str, str], str] = {}
    for app_label, config in self._apps.items():
        for declaration in config.routes:
            if not isinstance(declaration, Route):
                raise ImproperlyConfigured("AppConfig.routes must contain Route instances.")
            name = cast(str, declaration.name)
            route_key = (app_label, name)
            if route_key in self._routes:
                raise ImproperlyConfigured(f"Route {app_label}.{name} is already registered.")
            permission = declaration.permission
            if permission is not None and "." not in permission:
                permission = f"{app_label}.pages.{permission}"
            bound = replace(
                declaration,
                path=config.ui_path(self.settings) + declaration.path,
                permission=permission,
                title=declaration.title or name.replace("_", " ").title(),
            )
            scan = scan_template(
                self.templates.env, bound.template, base_template=self.settings.base_template
            )
            self._route_diagnostics.extend(
                Diagnostic(item.code, item.message) for item in scan.diagnostics
            )
            bindings: list[tuple[ComponentUse, ComponentService]] = []
            for use in scan.uses:
                identity = (app_label, use.signature)
                service = self._template_bindings.get(identity)
                if service is None:
                    module_name, separator, object_name = use.ref.rpartition(".")
                    if not separator:
                        raise ImproperlyConfigured(
                            f"Component reference {use.ref!r} must be an import path."
                        )
                    try:
                        declaration_obj = getattr(import_module(module_name), object_name)
                    except (ImportError, AttributeError) as exc:
                        raise ImproperlyConfigured(
                            f"Cannot import component {use.ref!r} from {use.template}:{use.line}."
                        ) from exc
                    component = (
                        declaration_obj()
                        if isinstance(declaration_obj, type)
                        and issubclass(declaration_obj, Component)
                        else declaration_obj
                    )
                    if not isinstance(component, Component):
                        raise ImproperlyConfigured(f"{use.ref!r} does not refer to a Component.")
                    declared_name = component.name or (
                        type(component).__name__[:1].lower() + type(component).__name__[1:]
                    )
                    component_name(declared_name)
                    if use.key is not None:
                        key = use.key
                    elif use.props:
                        digest = hashlib.sha1(
                            json.dumps(
                                use.props,
                                sort_keys=True,
                                separators=(",", ":"),
                                allow_nan=False,
                            ).encode(),
                            usedforsecurity=False,
                        ).hexdigest()[:8]
                        key = f"{declared_name}X{digest}"
                    else:
                        key = declared_name
                    component_name(key)
                    service = ComponentService.bind(
                        self,
                        declaration_obj,
                        app_label=app_label,
                        name=key,
                        title=use.props.get("title"),
                        filters=use.props.get("filters"),
                    )
                    if not service.definition.template and service.definition.plugin != "analytics":
                        raise ImproperlyConfigured(
                            f"Component {use.ref!r} requires a template or analytics renderer."
                        )
                    key_identity = (app_label, service.name)
                    previous = service_signatures.get(key_identity)
                    if previous is not None and previous != use.signature:
                        raise ImproperlyConfigured(
                            f"Template component key {service.name!r} is already in use."
                        )
                    service_signatures[key_identity] = use.signature
                    self._template_components[key_identity] = service
                    self._template_bindings[identity] = service
                elif use.key is not None and use.key != service.name:
                    raise ImproperlyConfigured(
                        f"Component {use.ref!r} uses conflicting keys in route {name!r}."
                    )
                bindings.append((use, service))
            self._routes[route_key] = bound
            self._route_components[route_key] = tuple(bindings)

    install_component_global(self.templates.env)
    for app_label, config in self._apps.items():
        app_routes = [route for (label, _), route in self._routes.items() if label == app_label]
        for route in app_routes:
            route_name = cast(str, route.name)
            view_base = route.view or TemplateView
            route_view = type(
                (
                    f"{app_label.title().replace('_', '')}"
                    f"{route_name.title().replace('_', '')}TemplateView"
                ),
                (RouteTemplateMixin, view_base),
                {
                    "_obox_runtime": self,
                    "_obox_app_label": app_label,
                    "_obox_route": route,
                },
            )
            router = APIRouter(route_class=HTMLRoute, include_in_schema=False)
            dependencies = (
                [Depends(permission_required(route.permission, html=True))]
                if route.permission is not None
                else []
            )
            cast(Any, route_view).as_route(
                router,
                route.path,
                name=route.route_name(app_label),
                dependencies=dependencies,
            )
            if any(
                self._conflict(candidate, other)
                for candidate in iter_route_contexts(router.routes)
                for other in iter_route_contexts(self.app.routes)
            ):
                raise ImproperlyConfigured(f"Route conflicts at {route.path!r}.")
            self.include_router(router, ui=True)
            if route.permission is None:
                self._public_routes.update(
                    id(candidate.original_route)
                    for candidate in iter_route_contexts(self.app.routes)
                    if candidate.name == route.route_name(app_label)
                )
        services = [
            service
            for (label, _), service in self._template_components.items()
            if label == app_label and service.query_model is not None
        ]
        if services:
            self.include_router(
                build_component_ui(self, app_label, services),
                ui=True,
                prefix=config.ui_path(self.settings),
            )
