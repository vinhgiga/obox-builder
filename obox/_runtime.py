"""FastAPI integration, builder registration and transaction ownership."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from copy import copy
from dataclasses import dataclass, replace
from inspect import isawaitable, iscoroutinefunction
from types import MappingProxyType
from typing import Any, TypeVar, cast

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.routing import RouteContext, iter_route_contexts
from markupsafe import escape
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import BaseRoute, Match, Mount
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from obox._auth import Permission, Principal
from obox._component_service import ComponentService
from obox._components import Component, component_name
from obox._config import RuntimeConfig
from obox._context import Context
from obox._database import Database
from obox._dependencies import _api_context, _api_contexts, _RoutePermission
from obox._exceptions import BuilderValidationError, OboxError, ResourceConflict
from obox._i18n import LANGUAGES, LanguageMiddleware
from obox._staticfiles import AssetMount
from obox.apps import AppConfig, validate_app_prefix
from obox.core.exceptions import ImproperlyConfigured
from obox.routes import Route

T = TypeVar("T")
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str


def _overlap(left: str, right: str) -> bool:
    a, b = left.strip("/").split("/"), right.strip("/").split("/")
    return len(a) == len(b) and all(
        x == y or x.startswith("{") or y.startswith("{") for x, y in zip(a, b, strict=True)
    )


async def _mark_app_api(request: Request) -> None:
    request.state.obox_app_api = True


class _RouteAuditMiddleware:
    def __init__(self, app: ASGIApp, *, runtime: _Runtime) -> None:
        self.app, self.runtime = app, runtime

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "lifespan" and not self.runtime._frozen:
            self.runtime.check(strict=True)
            self.runtime._frozen = True
        await self.app(scope, receive, send)


class _Runtime:
    def __init__(
        self,
        app: FastAPI,
        *,
        settings: RuntimeConfig | None = None,
        defer: bool = False,
        enforce_permissions: bool = False,
    ) -> None:
        from obox._csrf import CSRF
        from obox._ui import create_templates

        if getattr(app.state, "obox", None) is not None:
            raise ImproperlyConfigured("_Runtime is already installed on this application.")
        if app.middleware_stack is not None:
            raise ImproperlyConfigured("Install _Runtime before the application starts.")
        self.app = app
        self.settings = settings if settings is not None else RuntimeConfig.from_settings()

        original_openapi = app.openapi

        def openapi() -> dict[str, Any]:
            schema = original_openapi()
            from obox._routes import ApiRoute

            routes = tuple(iter_route_contexts(self.app.routes))
            if not self._features and not any(
                isinstance(route.original_route, ApiRoute) for route in routes
            ):
                return schema
            from obox._schemas import Problem

            definitions = Problem.model_json_schema(ref_template="#/components/schemas/{model}")
            components = schema.setdefault("components", {})
            schemas = components.setdefault("schemas", {})
            schemas.setdefault(
                "Problem", {key: value for key, value in definitions.items() if key != "$defs"}
            )
            schemas.update(definitions.get("$defs", {}))
            cookie_name = getattr(self.auth, "cookie_name", None)
            if isinstance(cookie_name, str) and cookie_name:
                components.setdefault("securitySchemes", {})["cookieAuth"] = {
                    "type": "apiKey",
                    "in": "cookie",
                    "name": cookie_name,
                }
                prefix = self.settings.api_prefix
                token_auth = bool(getattr(self.auth, "api_tokens", False))
                if token_auth:
                    components["securitySchemes"]["bearerAuth"] = {
                        "type": "http",
                        "scheme": "bearer",
                    }
                for route in routes:
                    if not isinstance(route.original_route, ApiRoute) and not any(
                        _api_contexts((route,))
                    ):
                        continue
                    path = route.path_format
                    if path is None:
                        continue
                    for method in route.methods or ():
                        operation = schema.get("paths", {}).get(path, {}).get(method.lower())
                        if operation is None:
                            continue
                        security: list[dict[str, list[str]]] = [{"cookieAuth": []}]
                        if token_auth and (
                            not path.startswith(prefix + "/auth/") or path == prefix + "/auth/me"
                        ):
                            security.append({"bearerAuth": []})
                        operation.setdefault("security", security)
            return schema

        cast(Any, app).openapi = openapi

        previous_http_handler = app.exception_handlers.get(StarletteHTTPException)

        async def http_error(request: Request, exc: Exception) -> Response:
            if isinstance(exc, OboxError):
                return await self._problem(request, exc)
            if previous_http_handler is not None:
                result = previous_http_handler(request, exc)
                return await result if isawaitable(result) else result
            return await http_exception_handler(request, cast(StarletteHTTPException, exc))

        app.add_exception_handler(StarletteHTTPException, http_error)
        app.exception_handlers.setdefault(OboxError, http_error)
        self.database = Database(self.settings)
        self.session_factory = self.database.sessions
        self._features: dict[str, Any] = {}
        self._loading_apps = True
        self._enforce_permissions = enforce_permissions
        self._public_routes: set[int] = set()
        self._resources: dict[tuple[str | None, type[Any]], type[Any]] = {}
        self._apps: dict[str, AppConfig] = {}
        self._routes: dict[tuple[str, str], Route] = {}
        self._components: dict[tuple[str, str], ComponentService] = {}
        self._template_components: dict[tuple[str, str], ComponentService] = {}
        self._template_bindings: dict[tuple[str, str], ComponentService] = {}
        self._route_components: dict[tuple[str, str], tuple[tuple[Any, ComponentService], ...]] = {}
        self._route_diagnostics: list[Diagnostic] = []
        self._installation_failed = False
        self.ui_prefixes = [self.settings.ui_prefix] if self.settings.ui_prefix else []
        self._ui_routes: list[BaseRoute | RouteContext] = []
        self._owned_routes: set[int] = set()
        self._frozen = False
        self.csrf = CSRF(self.settings.csrf_secret)
        self.templates = create_templates(self.settings)
        app.dependency_overrides.setdefault(_api_context, self.context_dependency("api"))
        from obox._identity import get_principal

        self._provided_principal = self.settings.principal_dependency
        if self._provided_principal is not None:
            app.dependency_overrides.setdefault(get_principal, self._provided_principal)
        original_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def lifespan(app: FastAPI) -> AsyncIterator[Any]:
            self.check(strict=True)
            self._frozen = True
            async with self.database.lifespan(), AsyncExitStack() as stack:
                for plugin in self._features.values():
                    if callable(getattr(plugin, "_lifespan", None)):
                        await stack.enter_async_context(plugin._lifespan(self))
                async with original_lifespan(app) as state:
                    self.check(strict=True)
                    yield state

        app.router.lifespan_context = lifespan
        app.add_middleware(LanguageMiddleware, prefixes=self.ui_prefixes, routes=self._ui_routes)
        if enforce_permissions:
            app.add_middleware(_RouteAuditMiddleware, runtime=self)
        self.mount_static(
            f"{self.settings.ui_prefix}/_static",
            namespace="builder",
            name="obox_static",
        )
        app.state.obox = self
        if defer:
            return
        try:
            self._loading_apps = False
            self._finish_apps()
            self._bind_templates()
        except BaseException:
            self._installation_failed = True
            raise

    @property
    def apps(self) -> Mapping[str, AppConfig]:
        return MappingProxyType(self._apps)

    @property
    def components(self) -> Mapping[tuple[str, str], ComponentService]:
        return MappingProxyType(self._components)

    def component(self, name: str, *, app_label: str) -> ComponentService:
        from obox._exceptions import NotFound

        try:
            return self._components[app_label, name]
        except KeyError as exc:
            raise NotFound("Unknown component.") from exc

    @property
    def permissions(self) -> Mapping[str, Permission]:
        result = {}
        from obox.contrib._models import installed_models
        from obox.db._identity import _model_label

        for model in installed_models():
            label, model_name = _model_label(model)
            for action in ("add", "change", "delete", "view", "comment"):
                name = f"{label}.{action}_{model_name}"
                result[name] = Permission(name, action.title(), "model")
        for model in self._resources.values():
            label, model_name = _model_label(model)
            for action in ("add", "change", "delete", "view"):
                name = f"{label}.{action}_{model_name}"
                result[name] = Permission(name, action.title(), "model")
        for route in self._routes.values():
            if route.permission is not None:
                result.setdefault(
                    route.permission,
                    Permission(route.permission, route.title or cast(str, route.name), "page"),
                )
        bindings = [
            *self._components.values(),
            *self._template_components.values(),
        ]
        for service in bindings:
            declaration = service.definition
            for permission in (declaration.permission, declaration.records_permission):
                if permission is not None:
                    result.setdefault(
                        permission, Permission(permission, declaration.title, "component")
                    )
        for dependency in _api_contexts(self.app.routes):
            if dependency.permission is not None:
                name = dependency.permission
                result.setdefault(name, Permission(name, name.rsplit(".", 1)[-1].title(), "api"))
        return MappingProxyType(result)

    def add_app(self, config: AppConfig) -> None:
        self._check_mutable()
        if not self._loading_apps:
            raise ImproperlyConfigured("Declare applications before app configuration is complete.")
        if not isinstance(config, AppConfig):
            raise ImproperlyConfigured("Register an AppConfig instance.")
        config = copy(config)
        config._registered_resources = []
        if not isinstance(config.name, str) or not config.name:
            raise ImproperlyConfigured("AppConfig.name must identify its Python package.")
        if (
            not isinstance(config.label, str)
            or re.fullmatch(r"[a-z][a-z0-9_]*", config.label) is None
            or config.label in LANGUAGES
        ):
            raise ImproperlyConfigured("Application labels must be lowercase identifiers.")
        if not isinstance(config.verbose_name, str):
            raise ImproperlyConfigured("AppConfig.verbose_name must be a string.")
        if (
            not isinstance(config.tab_bar_color, str)
            or re.fullmatch(r"#[\da-fA-F]{6}", config.tab_bar_color) is None
        ):
            raise ImproperlyConfigured("AppConfig.tab_bar_color must be a six-digit hex color.")
        for field in ("requires", "components", "routes", "routers"):
            if not isinstance(getattr(config, field), tuple):
                raise ImproperlyConfigured(f"AppConfig.{field} must be a tuple.")
        if any(not isinstance(router, APIRouter) for router in config.routers):
            raise ImproperlyConfigured("AppConfig.routers must contain APIRouter instances.")
        if any(not isinstance(dependency, str) for dependency in config.requires):
            raise ImproperlyConfigured("Application dependencies must be strings.")
        validate_app_prefix(config.api_prefix, "api_prefix")
        if config.label in self._apps or any(c.name == config.name for c in self._apps.values()):
            raise ImproperlyConfigured(f"Application {config.label!r} is already installed.")
        config.ui_path(self.settings)
        self._apps[config.label] = config

    def _check_apps(self) -> None:
        for config in self._apps.values():
            for dependency in config.requires:
                if dependency not in self._apps and dependency not in self._features:
                    raise ImproperlyConfigured(
                        f"Application {config.label!r} requires {dependency!r}."
                    )
            path = config.ui_path(self.settings)
            if path == self.settings.api_prefix or path.startswith(self.settings.api_prefix + "/"):
                raise ImproperlyConfigured(
                    "Application UI paths must not overlap the ApiOptions prefix."
                )
            if path.strip("/").split("/")[0] in LANGUAGES:
                raise ImproperlyConfigured("Application UI paths must not use language prefixes.")
            if any(
                other is not config and path.startswith(other.ui_path(self.settings) + "/")
                for other in self._apps.values()
            ):
                raise ImproperlyConfigured("Application UI prefixes must not overlap.")

    def _finish_apps(self) -> None:
        from obox._component_api import build_component_api
        from obox._navigation import build_catalog
        from obox._ui import create_templates

        self._check_apps()
        if self.settings.templates is None:
            self.templates.env.loader = create_templates(
                self.settings, tuple(self._apps.values())
            ).env.loader
        for config in self._apps.values():
            for declaration in config.components:
                service = ComponentService.bind(self, declaration, app_label=config.label)
                key = config.label, service.name
                if key in self._components:
                    raise ImproperlyConfigured(
                        "Component names must be unique within an application."
                    )
                self._components[key] = service
        for config in self._apps.values():
            if config.components:
                self.include_router(
                    build_component_api(self, config.label),
                    permission=f"{config.label}.components.access",
                )
            for router in config.routers:
                self.include_router(
                    router,
                    prefix=config.api_path(self.settings),
                    app_api=config.api_prefix is not None,
                )
            if config.has_directory("static"):
                route = Mount(
                    config.ui_path(self.settings) + "/_static",
                    app=StaticFiles(packages=[(config.name, "static")]),
                    name=f"obox_{config.label}_static",
                )
                self.app.routes.append(route)
                self._owned_routes.add(id(route))
                self._public_routes.add(id(route))
                self._ui_routes.append(route)
            self.add_ui_prefix(config.ui_path(self.settings))
        self.add_ui_prefix(self.settings.ui_prefix or "/")
        if any(config.routes or config._registered_resources for config in self._apps.values()):
            self.include_router(build_catalog(self), ui=True, permission="site.view_home")

    def _bind_templates(self) -> None:
        import hashlib
        import json
        from importlib import import_module

        from fastapi import APIRouter, Depends

        from obox._async import invoke
        from obox._component_scan import ComponentUse, scan_template
        from obox._component_ui import (
            build_component_ui,
            install_component_global,
            resolve_components,
            use_component_request,
        )
        from obox._dependencies import permission_required
        from obox._i18n import translate
        from obox.views.generic import TemplateView
        from obox.views.generic.base import HTMLRoute

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
                                f"Cannot import component {use.ref!r} from "
                                f"{use.template}:{use.line}."
                            ) from exc
                        component = (
                            declaration_obj()
                            if isinstance(declaration_obj, type)
                            and issubclass(declaration_obj, Component)
                            else declaration_obj
                        )
                        if not isinstance(component, Component):
                            raise ImproperlyConfigured(
                                f"{use.ref!r} does not refer to a Component."
                            )
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
                        if (
                            not service.definition.template
                            and service.definition.plugin != "analytics"
                        ):
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

    @property
    def routes(self) -> Mapping[tuple[str, str], Route]:
        return MappingProxyType(self._routes)

    @property
    def auth(self) -> Any:
        return self._feature("auth")

    def _feature(self, name: str) -> Any | None:
        return self._features.get(name)

    def _check_mutable(self) -> None:
        if self._installation_failed:
            raise ImproperlyConfigured("Application setup failed; create a new application.")
        if self._frozen or self.app.middleware_stack is not None:
            raise ImproperlyConfigured("Registration is frozen after application startup.")

    def _activate(self, plugin: Any) -> Any:
        self._check_mutable()
        lifespan_hook = getattr(plugin, "_lifespan", None)
        if (
            not callable(getattr(plugin, "_attach", None))
            or iscoroutinefunction(plugin._attach)
            or not isinstance(plugin.name, str)
            or re.fullmatch(r"[a-z][a-z0-9_-]*", plugin.name) is None
            or not isinstance(plugin.requires, tuple)
            or any(not isinstance(name, str) for name in plugin.requires)
            or (lifespan_hook is not None and not callable(lifespan_hook))
        ):
            raise ImproperlyConfigured("Invalid contrib service configuration.")
        name = plugin.name
        if name in self._features:
            raise ImproperlyConfigured(f"Any {name!r} is already installed.")
        for dependency in plugin.requires:
            if dependency not in self._features:
                raise ImproperlyConfigured(
                    f"Any {name!r} requires {dependency!r}; install it first."
                )
        self._features[name] = plugin
        self._attach_feature(plugin)
        return plugin

    def _attach_feature(self, plugin: Any) -> None:
        try:
            plugin._attach(self)
        except BaseException:
            del self._features[plugin.name]
            self._installation_failed = True
            raise

    def provide_defaults(
        self,
        *,
        principal_dependency: Callable[..., Any] | None = None,
    ) -> None:
        self._check_mutable()
        self.settings = replace(
            self.settings,
            principal_dependency=self.settings.principal_dependency
            if self.settings.principal_dependency is not None
            else principal_dependency,
        )
        from obox._identity import get_principal

        provided = self.app.dependency_overrides.get(get_principal)
        if provided is None or provided is self._provided_principal:
            self._provided_principal = self.settings.principal_dependency
            if self._provided_principal is not None:
                self.app.dependency_overrides[get_principal] = self._provided_principal
        if _api_context in self.app.dependency_overrides:
            self.app.dependency_overrides[_api_context] = self.context_dependency("api")

    def add_ui_prefix(self, prefix: str) -> None:
        self._check_mutable()
        if not isinstance(prefix, str):
            raise ImproperlyConfigured("UI prefixes must be nonempty absolute URL paths.")
        prefix = prefix.rstrip("/") or "/"
        if not prefix.startswith("/") or any(char in prefix for char in "{}?#"):
            raise ImproperlyConfigured("UI prefixes must be nonempty absolute URL paths.")
        if prefix not in self.ui_prefixes:
            self.ui_prefixes.append(prefix)

    def mount_static(self, path: str, *, namespace: str, name: str) -> None:
        self._check_mutable()
        route = AssetMount(path, namespace=namespace, name=name, root=self.settings.static_root)
        self.app.routes.append(route)
        self._owned_routes.add(id(route))
        self._public_routes.add(id(route))
        self._ui_routes.append(route)

    @staticmethod
    async def _problem(request: Request, exc: Exception) -> Response:
        from obox._routes import authentication_response, error_status

        obox = request.app.state.obox
        path = request.scope.get("obox_path", "")
        error = exc if isinstance(exc, OboxError) else OboxError(str(exc))
        body: dict[str, Any] = {
            "type": "about:blank",
            "title": type(error).__name__,
            "status": error_status(error),
            "detail": str(error),
            "instance": request.url.path,
        }
        if isinstance(error, BuilderValidationError):
            body["errors"] = error.errors
        html = bool(getattr(request.state, "obox_html_permission", False))
        response = (
            HTMLResponse(escape(str(error)), status_code=error_status(error))
            if html and request.headers.get("x-obox-request") != "true"
            else JSONResponse(
                body, status_code=error_status(error), media_type="application/problem+json"
            )
        )
        return authentication_response(
            request,
            exc,
            response,
            ui=html
            or any(path == prefix or path.startswith(prefix + "/") for prefix in obox.ui_prefixes),
        )

    @staticmethod
    async def _validation_problem(request: Request, exc: Exception) -> JSONResponse:
        errors = exc.errors() if isinstance(exc, RequestValidationError) else []
        return JSONResponse(
            {
                "type": "about:blank",
                "title": "Invalid input",
                "status": 422,
                "instance": request.url.path,
                "errors": [{"loc": list(error["loc"]), "msg": error["msg"]} for error in errors],
            },
            status_code=422,
            media_type="application/problem+json",
        )

    def include_router(
        self,
        router: APIRouter,
        *,
        ui: bool = False,
        prefix: str = "",
        permission: str | None = None,
        check_csrf: bool = False,
        app_api: bool = False,
    ) -> None:
        self._check_mutable()
        dependencies = [Depends(_mark_app_api)] if app_api else []
        if self._enforce_permissions and permission:
            dependencies.append(Depends(_RoutePermission(permission, ui, check_csrf)))
        self.app.include_router(router, prefix=prefix, dependencies=dependencies or None)
        self.app.openapi_schema = None
        owned = {id(r.original_route) for r in iter_route_contexts(router.routes)}
        self._owned_routes.update(owned)
        if ui:
            self._ui_routes.extend(
                r for r in iter_route_contexts(self.app.routes) if id(r.original_route) in owned
            )

    def _audit_routes(self) -> None:
        routes = list(iter_route_contexts(self.app.routes))
        from obox.api import _check_route_conflicts

        _check_route_conflicts(routes, all_routes=True)
        for i, route in enumerate(routes):
            if id(route.original_route) in self._public_routes:
                continue
            methods = route.methods or ()
            if not methods or not any(dep.permission for dep in _api_contexts((route,))):
                raise ImproperlyConfigured(
                    f"Missing route permission: {', '.join(sorted(methods))} {route.path}."
                )
            if not route.path:
                continue
            generated = (route.name or "").startswith(("obox:api:", "obox:views:"))
            dynamic = "{" in route.path
            target = re.sub(
                r"\{[^}:]+(?::([^}]+))?\}",
                lambda match: {
                    "uuid": "01234567-89ab-4def-8123-0123456789ab",
                    "float": "1.0",
                }.get(match[1], "1"),
                route.path,
            )
            for previous in routes[:i]:
                if not (
                    generated or (previous.name or "").startswith(("obox:api:", "obox:views:"))
                ):
                    continue
                if dynamic and "{" not in (previous.path or ""):
                    continue
                if any(
                    previous.matches({"type": "http", "path": target, "method": method})[0]
                    is Match.FULL
                    for method in methods
                ):
                    raise ImproperlyConfigured(
                        f"Route is shadowed: {route.path} by {previous.path}."
                    )

    @staticmethod
    def _conflict(
        left: Any, right: Any, *, internal: bool = False, path: str | None = None
    ) -> bool:
        if path is None and left.name and left.name == right.name:
            return True
        left_path = left.path if path is None else path
        if not left_path or not right.path:
            return False
        if isinstance(right.original_route, Mount):
            return bool(
                left_path == right.path or left_path.startswith(right.path.rstrip("/") + "/")
            )
        if isinstance(left.original_route, Mount):
            return bool(
                right.path == left_path or right.path.startswith(left_path.rstrip("/") + "/")
            )
        if not set(left.methods or ()).intersection(right.methods or ()):
            return False
        if internal and left_path != right.path:
            # Generated /new precedes /{id}; both are intentional within one resource.
            return False
        return _overlap(left_path, right.path)

    def check(self, *, strict: bool = False) -> Sequence[Diagnostic]:
        if self._installation_failed:
            raise ImproperlyConfigured("Application setup failed.")
        from jinja2 import TemplateNotFound

        from obox.api import _check_route_conflicts

        if self._enforce_permissions:
            self._audit_routes()
        else:
            _check_route_conflicts(iter_route_contexts(self.app.routes))

        diagnostics = list(self._route_diagnostics)
        definitions = [
            *[service.definition for service in self._components.values()],
            *[service.definition for service in self._template_components.values()],
        ]
        names = [definition.template for definition in definitions if definition.template]
        for name in names:
            try:
                self.templates.env.get_template(name)
            except TemplateNotFound:
                diagnostics.append(Diagnostic("missing_template", f"Missing template: {name}"))
        errors = [item for item in diagnostics if item.code != "dynamic_template_reference"]
        if strict and errors:
            raise ImproperlyConfigured("; ".join(item.message for item in errors))
        return tuple(diagnostics)

    @asynccontextmanager
    async def uow(
        self,
        *,
        principal: Any,
        source: str = "job",
        request: Request | None = None,
        locale: str | None = None,
        trace_id: str | None = None,
    ) -> AsyncIterator[Context]:
        ctx = None
        committing = False
        try:
            self.database.check()
            async with self.session_factory(
                expire_on_commit=False, close_resets_only=False
            ) as session:
                ctx = Context(
                    principal=principal,
                    session=session,
                    request=request,
                    source=source,
                    locale=locale,
                    trace_id=trace_id,
                    _owner=self,
                    _active=True,
                )
                session.info["obox._context"] = ctx

                def before_commit(_: Any) -> None:
                    if not committing and not session.in_nested_transaction():
                        raise ImproperlyConfigured("Only the unit of work may commit.")

                event.listen(session.sync_session, "before_commit", before_commit)
                try:
                    async with session.begin():
                        ctx._transaction = session.sync_session.get_transaction()
                        from obox.db import _current

                        token = _current.set((asyncio.current_task(), session))
                        try:
                            yield ctx
                        finally:
                            _current.reset(token)
                        if (
                            ctx._failed
                            or session.sync_session.get_transaction() is not ctx._transaction
                        ):
                            raise ImproperlyConfigured(
                                "The failed unit of work must be rolled back."
                            )
                        committing = True
                finally:
                    ctx._active = False
        except (IntegrityError, StaleDataError) as exc:
            raise ResourceConflict(
                "The transaction conflicts with current database state."
            ) from exc

    def context_dependency(self, source: str) -> Callable[..., Any]:
        def request_principal(request: Request) -> Any:
            return request.scope.get("user", Principal())

        resolver = self.settings.principal_dependency or request_principal

        async def dependency(
            request: Request,
            principal: Any = Depends(resolver),
        ) -> AsyncIterator[Context]:
            async with self.uow(
                principal=principal,
                source=source,
                request=request,
                trace_id=getattr(request.state, "trace_id", None),
            ) as ctx:
                yield ctx

        return dependency
