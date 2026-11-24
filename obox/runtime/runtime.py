"""FastAPI integration, builder registration and transaction ownership."""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from copy import copy
from dataclasses import replace
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

from obox.apps import AppConfig, validate_app_prefix
from obox.components.binding import Diagnostic
from obox.components.service import ComponentService
from obox.core.exceptions import (
    BuilderValidationError,
    ImproperlyConfigured,
    OboxError,
    ResourceConflict,
)
from obox.core.principal import Permission, Principal
from obox.db.engine import Database
from obox.db.session import bind_session
from obox.routes import Route
from obox.runtime.config import RuntimeConfig
from obox.runtime.context import Context
from obox.web.dependencies import _RoutePermission, api_context_dependency, iter_api_contexts
from obox.web.i18n import LANGUAGES, LanguageMiddleware
from obox.web.staticfiles import AssetMount

T = TypeVar("T")
logger = logging.getLogger(__name__)


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
        from obox.web.csrf import CSRF
        from obox.web.ui import create_templates

        if getattr(app.state, "obox", None) is not None:
            raise ImproperlyConfigured("_Runtime is already installed on this application.")
        if app.middleware_stack is not None:
            raise ImproperlyConfigured("Install _Runtime before the application starts.")
        self.app = app
        self.settings = settings if settings is not None else RuntimeConfig.from_settings()

        original_openapi = app.openapi

        def openapi() -> dict[str, Any]:
            schema = original_openapi()
            from obox.web.routes import ApiRoute

            routes = tuple(iter_route_contexts(self.app.routes))
            if not self._features and not any(
                isinstance(route.original_route, ApiRoute) for route in routes
            ):
                return schema
            from obox.web.schemas import Problem

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
                        iter_api_contexts((route,))
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
                return await self.problem(request, exc)
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
        app.dependency_overrides.setdefault(api_context_dependency, self.context_dependency("api"))
        from obox.web.identity import get_principal

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
                    if callable(getattr(plugin, "lifespan", None)):
                        await stack.enter_async_context(plugin.lifespan(self))
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
            self.finish_apps()
            self.bind_templates()
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
        from obox.core.exceptions import NotFound

        try:
            return self._components[app_label, name]
        except KeyError as exc:
            raise NotFound("Unknown component.") from exc

    @property
    def permissions(self) -> Mapping[str, Permission]:
        result = {}
        from obox.contrib._models import installed_models
        from obox.db.identity import model_label

        for model in installed_models():
            label, model_name = model_label(model)
            for action in ("add", "change", "delete", "view", "comment"):
                name = f"{label}.{action}_{model_name}"
                result[name] = Permission(name, action.title(), "model")
        for model in self._resources.values():
            label, model_name = model_label(model)
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
        for dependency in iter_api_contexts(self.app.routes):
            if dependency.permission is not None:
                name = dependency.permission
                result.setdefault(name, Permission(name, name.rsplit(".", 1)[-1].title(), "api"))
        return MappingProxyType(result)

    def add_app(self, config: AppConfig) -> None:
        self.check_mutable()
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

    def finish_apps(self) -> None:
        from obox.components.api import build_component_api
        from obox.web.navigation import build_catalog
        from obox.web.ui import create_templates

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

    def bind_templates(self) -> None:
        from obox.components.binding import bind_templates

        bind_templates(self)

    @property
    def routes(self) -> Mapping[tuple[str, str], Route]:
        return MappingProxyType(self._routes)

    @property
    def auth(self) -> Any:
        return self.feature("auth")

    def feature(self, name: str) -> Any | None:
        return self._features.get(name)

    def check_mutable(self) -> None:
        if self._installation_failed:
            raise ImproperlyConfigured("Application setup failed; create a new application.")
        if self._frozen or self.app.middleware_stack is not None:
            raise ImproperlyConfigured("Registration is frozen after application startup.")

    def activate(self, plugin: Any) -> Any:
        self.check_mutable()
        lifespan_hook = getattr(plugin, "lifespan", None)
        if (
            not callable(getattr(plugin, "attach", None))
            or iscoroutinefunction(plugin.attach)
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
            plugin.attach(self)
        except BaseException:
            del self._features[plugin.name]
            self._installation_failed = True
            raise

    def provide_defaults(
        self,
        *,
        principal_dependency: Callable[..., Any] | None = None,
    ) -> None:
        self.check_mutable()
        self.settings = replace(
            self.settings,
            principal_dependency=self.settings.principal_dependency
            if self.settings.principal_dependency is not None
            else principal_dependency,
        )
        from obox.web.identity import get_principal

        provided = self.app.dependency_overrides.get(get_principal)
        if provided is None or provided is self._provided_principal:
            self._provided_principal = self.settings.principal_dependency
            if self._provided_principal is not None:
                self.app.dependency_overrides[get_principal] = self._provided_principal
        if api_context_dependency in self.app.dependency_overrides:
            self.app.dependency_overrides[api_context_dependency] = self.context_dependency("api")

    def add_ui_prefix(self, prefix: str) -> None:
        self.check_mutable()
        if not isinstance(prefix, str):
            raise ImproperlyConfigured("UI prefixes must be nonempty absolute URL paths.")
        prefix = prefix.rstrip("/") or "/"
        if not prefix.startswith("/") or any(char in prefix for char in "{}?#"):
            raise ImproperlyConfigured("UI prefixes must be nonempty absolute URL paths.")
        if prefix not in self.ui_prefixes:
            self.ui_prefixes.append(prefix)

    def mount_static(self, path: str, *, namespace: str, name: str) -> None:
        self.check_mutable()
        route = AssetMount(path, namespace=namespace, name=name, root=self.settings.static_root)
        self.app.routes.append(route)
        self._owned_routes.add(id(route))
        self._public_routes.add(id(route))
        self._ui_routes.append(route)

    @staticmethod
    async def problem(request: Request, exc: Exception) -> Response:
        from obox.web.routes import authentication_response, error_status

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
    async def validation_problem(request: Request, exc: Exception) -> JSONResponse:
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
        self.check_mutable()
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
        from obox.api import check_route_conflicts

        check_route_conflicts(routes, all_routes=True)
        for i, route in enumerate(routes):
            if id(route.original_route) in self._public_routes:
                continue
            methods = route.methods or ()
            if not methods or not any(dep.permission for dep in iter_api_contexts((route,))):
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

        from obox.api import check_route_conflicts

        if self._enforce_permissions:
            self._audit_routes()
        else:
            check_route_conflicts(iter_route_contexts(self.app.routes))

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
                session.info["obox.runtime.context"] = ctx

                def before_commit(_: Any) -> None:
                    if not committing and not session.in_nested_transaction():
                        raise ImproperlyConfigured("Only the unit of work may commit.")

                event.listen(session.sync_session, "before_commit", before_commit)
                try:
                    async with session.begin():
                        ctx._transaction = session.sync_session.get_transaction()
                        async with bind_session(session):
                            yield ctx
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
