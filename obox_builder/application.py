"""FastAPI integration, builder registration and transaction ownership."""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, replace
from inspect import isawaitable, iscoroutinefunction
from types import MappingProxyType, new_class
from typing import Any, Generic, TypeVar, cast, overload
from uuid import uuid4

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import RouteContext, iter_route_contexts
from sqlalchemy import event, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapper
from sqlalchemy.orm.exc import StaleDataError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import BaseRoute, Mount
from starlette.staticfiles import StaticFiles

from obox_builder.apps import AppConfig
from obox_builder.auth import Permission, Policy, Principal
from obox_builder.builder import ModelBuilder
from obox_builder.builder.service import ResourceService, invoke
from obox_builder.conf import Settings, load_settings
from obox_builder.context import Context
from obox_builder.db import Database
from obox_builder.errors import (
    BuilderValidationError,
    ImproperlyConfigured,
    OboxError,
    ResourceConflict,
)
from obox_builder.i18n import LANGUAGES, LanguageMiddleware, negotiate
from obox_builder.pages import Page
from obox_builder.plugin import LifespanPlugin, Plugin
from obox_builder.staticfiles import AssetMount

T = TypeVar("T")
P = TypeVar("P", bound=Plugin)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Diagnostic:
    level: str
    code: str
    message: str
    exposure: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class Registration(Generic[T]):
    model: type[T]
    builder: ModelBuilder[T]
    service: ResourceService[T]
    slug: str
    routes: tuple[str, ...]
    app_label: str | None = None


def _overlap(left: str, right: str) -> bool:
    a, b = left.strip("/").split("/"), right.strip("/").split("/")
    return len(a) == len(b) and all(
        x == y or x.startswith("{") or y.startswith("{") for x, y in zip(a, b, strict=True)
    )


class Obox:
    def __init__(
        self,
        app: FastAPI,
        *,
        settings: Settings | str | None = None,
        plugins: Sequence[Plugin] = (),
    ) -> None:
        from obox_builder.builder.csrf import CSRF
        from obox_builder.builder.ui import create_templates

        if getattr(app.state, "obox", None) is not None:
            raise ImproperlyConfigured("Obox is already installed on this application.")
        if app.middleware_stack is not None:
            raise ImproperlyConfigured("Install Obox before the application starts.")
        self.app = app
        self.settings = load_settings(settings)

        original_openapi = app.openapi

        def openapi() -> dict[str, Any]:
            schema = original_openapi()
            from obox_builder.builder.api_schema import Problem

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
                for path, operations in schema.get("paths", {}).items():
                    if path == prefix or path.startswith((prefix + "/", prefix + ":")):
                        for operation in operations.values():
                            security: list[dict[str, list[str]]] = [{"cookieAuth": []}]
                            if token_auth and (
                                not path.startswith(prefix + "/auth/")
                                or path == prefix + "/auth/me"
                            ):
                                security.append({"bearerAuth": []})
                            operation.setdefault("security", security)
            return schema

        cast(Any, app).openapi = openapi

        previous_http_handler = app.exception_handlers.get(StarletteHTTPException)

        async def http_error(request: Request, exc: Exception) -> Response:
            path = request.scope.get("path", "")
            api_prefix = self.settings.api_prefix
            if path == api_prefix or path.startswith((api_prefix + "/", api_prefix + ":")):
                from obox_builder.builder.http import api_problem

                provided = request.headers.get("x-request-id", "")
                request.state.trace_id = (
                    provided if re.fullmatch(r"[A-Za-z0-9._-]{1,128}", provided) else uuid4().hex
                )
                request.state.language = negotiate(request.headers.get("accept-language", ""))
                response = await api_problem(request, exc)
                response.headers["X-Request-ID"] = request.state.trace_id
                response.headers["Cache-Control"] = "private, no-store"
                response.headers["Vary"] = "Cookie, Authorization, Accept-Language"
                response.headers["Content-Language"] = request.state.language
                return response
            if previous_http_handler is not None:
                result = previous_http_handler(request, exc)
                return await result if isawaitable(result) else result
            return await http_exception_handler(request, cast(StarletteHTTPException, exc))

        app.add_exception_handler(StarletteHTTPException, http_error)
        self.database = Database(self.settings)
        self.session_factory = self.database.sessions
        self._plugins: dict[str, Plugin] = {}
        self._pending_plugins: list[Plugin] = []
        self._loading_apps = True
        self._apps: dict[str, AppConfig] = {}
        self._pages: dict[tuple[str, str], Page] = {}
        self.catalog_enabled = self.settings.installed_apps is not None
        self._installation_failed = False
        self.ui_prefixes = [self.settings.ui_prefix] if self.settings.ui_prefix else []
        self._ui_routes: list[BaseRoute | RouteContext] = []
        self._registrations: dict[type[Any], Registration[Any]] = {}
        self._generated: dict[Callable[..., Any] | None, type[Any]] = {}
        self._owned_routes: set[int] = set()
        self._frozen = False
        self.csrf = CSRF(self.settings.csrf_secret)
        self.templates = create_templates(self.settings)
        original_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def lifespan(app: FastAPI) -> AsyncIterator[Any]:
            self.check(strict=True)
            self._frozen = True
            async with self.database.lifespan(), AsyncExitStack() as stack:
                for plugin in self._plugins.values():
                    if isinstance(plugin, LifespanPlugin):
                        await stack.enter_async_context(plugin.lifespan(self))
                async with original_lifespan(app) as state:
                    self.check(strict=True)
                    yield state

        app.router.lifespan_context = lifespan
        app.add_middleware(LanguageMiddleware, prefixes=self.ui_prefixes, routes=self._ui_routes)
        self.mount_static(
            f"{self.settings.ui_prefix}/_static",
            namespace="builder",
            name="obox_static",
        )
        app.state.obox = self
        for plugin in plugins:
            self.install(plugin)
        try:
            self._load_apps()
            self._loading_apps = False
            for plugin in self._pending_plugins:
                self._setup_plugin(plugin)
            self._pending_plugins.clear()
        except BaseException:
            self._installation_failed = True
            raise

    @property
    def apps(self) -> Mapping[str, AppConfig]:
        return MappingProxyType(self._apps)

    @property
    def pages(self) -> Mapping[tuple[str, str], Page]:
        return MappingProxyType(self._pages)

    @property
    def permissions(self) -> Mapping[str, Permission]:
        result = {}
        for registration in self._registrations.values():
            builder = registration.builder
            for action in ("list", "retrieve", "create", "update", "delete", *builder.permissions):
                name = builder.permission_name(action)
                result[name] = Permission(name, action.replace("_", " ").title(), "model")
        for page in self._pages.values():
            if page.permission is not None:
                result.setdefault(
                    page.permission,
                    Permission(page.permission, page.title or page.name, "page"),
                )
        return MappingProxyType(result)

    def add_page(self, page: Page, *, app_label: str) -> None:
        from obox_builder.builder.pages import build_page

        self._check_mutable()
        if not isinstance(page, Page):
            raise ImproperlyConfigured("Register a Page declaration.")
        if app_label not in self._apps:
            raise ImproperlyConfigured(f"Unknown application {app_label!r}.")
        if (app_label, page.name) in self._pages:
            raise ImproperlyConfigured(f"Page {app_label}.{page.name} is already registered.")
        if not page.public and not callable(self.settings.principal_dependency):
            raise ImproperlyConfigured("Protected pages require an identity resolver.")
        config = self._apps[app_label]
        bound = replace(
            page,
            path=config.ui_path(self.settings) + (page.path or f"/{page.name}"),
            template=page.template or f"{app_label}/{page.name}.html",
            title=page.title or page.name.replace("_", " ").title(),
        )
        router = build_page(self, bound, app_label)
        for candidate in iter_route_contexts(router.routes):
            if any(
                self._conflict(candidate, other) for other in iter_route_contexts(self.app.routes)
            ):
                raise ImproperlyConfigured(f"Page route conflicts at {candidate.path!r}.")
        self.include_router(router, ui=True)
        self._pages[app_label, page.name] = bound
        self.add_ui_prefix(config.ui_path(self.settings))

    def add_app(self, config: AppConfig) -> None:
        self._check_mutable()
        if self.catalog_enabled and not self._loading_apps:
            raise ImproperlyConfigured("Declare applications before app configuration is complete.")
        config = AppConfig.load(config)
        if config.label in self._apps or any(c.name == config.name for c in self._apps.values()):
            raise ImproperlyConfigured(f"Application {config.label!r} is already installed.")
        config.ui_path(self.settings)
        self._apps[config.label] = config
        if not self._loading_apps and self.settings.templates is None:
            from obox_builder.builder.ui import create_templates

            self.templates.env.loader = create_templates(
                self.settings, tuple(self._apps.values())
            ).env.loader

    def _load_apps(self) -> None:
        from obox_builder.builder.catalog import build_catalog
        from obox_builder.builder.ui import create_templates

        for entry in self.settings.installed_apps or ():
            self.add_app(AppConfig.load(entry))
        for config in self._apps.values():
            for dependency in config.requires:
                if dependency not in self._apps and dependency not in self._plugins:
                    raise ImproperlyConfigured(
                        f"Application {config.label!r} requires {dependency!r}."
                    )
            path = config.ui_path(self.settings)
            if path == self.settings.api_prefix or path.startswith(self.settings.api_prefix + "/"):
                raise ImproperlyConfigured("Application UI paths must not overlap the API prefix.")
            if path.strip("/").split("/")[0] in LANGUAGES:
                raise ImproperlyConfigured("Application UI paths must not use language prefixes.")
            if any(
                other is not config and path.startswith(other.ui_path(self.settings) + "/")
                for other in self._apps.values()
            ):
                raise ImproperlyConfigured("Application UI prefixes must not overlap.")
        for config in self._apps.values():
            for builder in config.builders:
                self.register(builder, app_label=config.label)
            for page in config.pages:
                self.add_page(page, app_label=config.label)
            if config.ui_router is not None:
                self.include_router(config.ui_router, prefix=config.ui_path(self.settings), ui=True)
            if config.api_router is not None:
                self.include_router(
                    config.api_router, prefix=f"{self.settings.api_prefix}/{config.label}"
                )
            if config.has_directory("static"):
                route = Mount(
                    config.ui_path(self.settings) + "/_static",
                    app=StaticFiles(packages=[(config.name, "static")]),
                    name=f"obox_{config.label}_static",
                )
                self.app.routes.append(route)
                self._owned_routes.add(id(route))
                self._ui_routes.append(route)
            self.add_ui_prefix(config.ui_path(self.settings))
        if self.settings.templates is None:
            self.templates.env.loader = create_templates(
                self.settings, tuple(self._apps.values())
            ).env.loader
        if self.catalog_enabled:
            self.add_ui_prefix(self.settings.ui_prefix or "/")
            self.include_router(build_catalog(self), ui=True)

    @property
    def plugins(self) -> Mapping[str, Plugin]:
        return MappingProxyType(self._plugins)

    @property
    def auth(self) -> Any:
        return self.get_plugin("auth")

    def get_plugin(self, name: str) -> Plugin | None:
        return self._plugins.get(name)

    def _check_mutable(self) -> None:
        if self._installation_failed:
            raise ImproperlyConfigured("Plugin setup failed; create a new application.")
        if self._frozen or self.app.middleware_stack is not None:
            raise ImproperlyConfigured("Registration is frozen after application startup.")

    def install(self, plugin: P) -> P:
        self._check_mutable()
        lifespan_hook = getattr(plugin, "lifespan", None)
        if (
            not isinstance(plugin, Plugin)
            or not callable(plugin.setup)
            or iscoroutinefunction(plugin.setup)
            or not isinstance(plugin.name, str)
            or re.fullmatch(r"[a-z][a-z0-9_-]*", plugin.name) is None
            or not isinstance(plugin.requires, tuple)
            or any(not isinstance(name, str) for name in plugin.requires)
            or (lifespan_hook is not None and not callable(lifespan_hook))
        ):
            raise ImproperlyConfigured(
                "Install a plugin with a name, requires tuple and setup method."
            )
        name = plugin.name
        if name in self._plugins:
            raise ImproperlyConfigured(f"Plugin {name!r} is already installed.")
        for dependency in plugin.requires:
            if dependency not in self._plugins:
                raise ImproperlyConfigured(
                    f"Plugin {name!r} requires {dependency!r}; install it first."
                )
        self._plugins[name] = plugin
        if self._loading_apps and getattr(plugin, "requires_resources", False):
            self._pending_plugins.append(plugin)
        else:
            self._setup_plugin(plugin)
        return plugin

    def _setup_plugin(self, plugin: Plugin) -> None:
        try:
            plugin.setup(self)
        except BaseException:
            del self._plugins[plugin.name]
            self._installation_failed = True
            raise

    def provide_defaults(
        self,
        *,
        principal_dependency: Callable[..., Any] | None = None,
        default_policy: Policy[Any] | None = None,
    ) -> None:
        self._check_mutable()
        self.settings = replace(
            self.settings,
            principal_dependency=self.settings.principal_dependency
            if self.settings.principal_dependency is not None
            else principal_dependency,
            default_policy=self.settings.default_policy
            if self.settings.default_policy is not None
            else default_policy,
        )

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
        self._ui_routes.append(route)

    @staticmethod
    async def _problem(request: Request, exc: Exception) -> Response:
        from obox_builder.builder.http import authentication_response

        obox = request.app.state.obox
        path = request.scope.get("obox_path", "")
        error = exc if isinstance(exc, OboxError) else OboxError(str(exc))
        body: dict[str, Any] = {
            "type": "about:blank",
            "title": type(error).__name__,
            "status": error.status,
            "detail": str(error),
            "instance": request.url.path,
        }
        if isinstance(error, BuilderValidationError):
            body["errors"] = error.errors
        return authentication_response(
            request,
            exc,
            JSONResponse(body, status_code=error.status, media_type="application/problem+json"),
            ui=any(path == prefix or path.startswith(prefix + "/") for prefix in obox.ui_prefixes),
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

    @overload
    def register(
        self, builder: type[ModelBuilder[T]], *, app_label: str | None = None
    ) -> Registration[T]: ...

    @overload
    def register(self, builder: type[T], *, app_label: str | None = None) -> Registration[T]: ...

    def register(self, builder: type[Any], *, app_label: str | None = None) -> Registration[Any]:
        from obox_builder.builder.api import build_api
        from obox_builder.builder.ui import build_ui

        self._check_mutable()
        if app_label is not None and app_label not in self._apps:
            raise ImproperlyConfigured(f"Unknown application {app_label!r}.")
        if self.catalog_enabled and app_label is None:
            raise ImproperlyConfigured("Register catalog resources with an application label.")
        if isinstance(builder, type) and isinstance(inspect(builder, raiseerr=False), Mapper):
            model_type = builder
            builder = new_class(
                f"{model_type.__name__}Builder",
                (cast(Any, ModelBuilder)[model_type],),
                exec_body=lambda namespace: namespace.update(__module__=model_type.__module__),
            )
        if not isinstance(builder, type) or not issubclass(builder, ModelBuilder):
            raise ImproperlyConfigured("Register a ModelBuilder subclass.")
        if getattr(builder, "model", None) is not None:
            raise ImproperlyConfigured(
                "Builder.model is no longer supported; specialize ModelBuilder[Model]."
            )
        definition = builder()
        definition.app_label = app_label
        model = builder._model_type()
        if model is None:
            raise ImproperlyConfigured(
                "Builder must specialize ModelBuilder with one concrete model class."
            )
        if not callable(self.settings.principal_dependency) or not isinstance(
            self.settings.default_policy, Policy
        ):
            raise ImproperlyConfigured("Registration requires authentication and a default policy.")
        if model in self._registrations:
            raise ImproperlyConfigured("This model is already registered.")
        definition._compile(model, self.settings.default_policy, self)
        if definition.slug is None:
            raise ImproperlyConfigured("The compiled builder has no slug.")
        if any(
            item.slug == definition.slug and item.app_label == app_label
            for item in self._registrations.values()
        ):
            raise ImproperlyConfigured(f"Duplicate resource slug: {definition.slug!r}.")
        service = ResourceService[Any](definition, self)
        routers = (build_api(self, definition, service), build_ui(self, definition, service))
        candidates = [route for router in routers for route in iter_route_contexts(router.routes)]
        existing = list(iter_route_contexts(self.app.routes))
        for i, candidate in enumerate(candidates):
            for other in (*existing, *candidates[:i]):
                if self._conflict(candidate, other, internal=other in candidates[:i]):
                    raise ImproperlyConfigured(f"Generated route conflicts at {candidate.path!r}.")
        registration = Registration(
            model,
            definition,
            service,
            definition.slug,
            tuple(route.path for route in candidates if route.path is not None),
            app_label,
        )
        self._registrations[model] = registration
        for route in candidates:
            self._generated[route.endpoint] = model
        for router in routers:
            self.include_router(router, ui=router is routers[1])
        self.app.openapi_schema = None
        return registration

    def include_router(self, router: APIRouter, *, ui: bool = False, prefix: str = "") -> None:
        self._check_mutable()
        self.app.include_router(router, prefix=prefix)
        owned = {id(r.original_route) for r in iter_route_contexts(router.routes)}
        self._owned_routes.update(owned)
        if ui:
            self._ui_routes.extend(
                r for r in iter_route_contexts(self.app.routes) if id(r.original_route) in owned
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

    def get_builder(self, model: type[T]) -> ModelBuilder[T]:
        return self._registrations[model].builder

    def service(self, model: type[T]) -> ResourceService[T]:
        return self._registrations[model].service

    def _for_table(self, table: Any) -> Any:
        import sqlalchemy as sa

        matches = [
            item.builder
            for item in self._registrations.values()
            if table in sa.inspect(item.model).tables
        ]
        if len(matches) > 1:
            raise ImproperlyConfigured("A foreign-key target table maps to multiple resources.")
        return matches[0] if matches else None

    def check(self, *, strict: bool = False) -> Sequence[Diagnostic]:
        from obox_builder.auth import Policy

        if self._installation_failed:
            raise ImproperlyConfigured("Plugin setup failed; create a new application.")
        diagnostics = [
            Diagnostic(
                "error",
                "app.analytics_missing",
                f"{config.label}: install Analytics to enable its dashboards.",
            )
            for config in self._apps.values()
            if config.dashboards and self.get_plugin("analytics") is None
        ]
        from jinja2 import TemplateError

        for (label, name), page in self._pages.items():
            try:
                self.templates.env.get_template(cast(str, page.template))
            except TemplateError as exc:
                diagnostics.append(Diagnostic("error", "page.template", f"{label}.{name}: {exc}"))
        routes = list(iter_route_contexts(self.app.routes))
        for i, route in enumerate(routes):
            for other in routes[:i]:
                model = self._generated.get(route.endpoint)
                other_model = self._generated.get(other.endpoint)
                if not any(id(r.original_route) in self._owned_routes for r in (route, other)):
                    continue
                internal = model is not None and model is other_model
                if self._conflict(route, other, internal=internal):
                    diagnostics.append(
                        Diagnostic("error", "route.conflict", f"Route conflict: {route.path}.")
                    )
                for owned, host in ((route, other), (other, route)):
                    if id(owned.original_route) not in self._owned_routes or not owned.path:
                        continue
                    if not any(
                        owned.path == p or owned.path.startswith(p + "/") for p in self.ui_prefixes
                    ):
                        continue
                    if host.path and any(
                        self._conflict(owned, host, path=f"/{language}{owned.path}")
                        for language in LANGUAGES
                    ):
                        diagnostics.append(
                            Diagnostic("error", "route.conflict", f"Route conflict: {host.path}.")
                        )
        for item in self._registrations.values():
            b = item.builder
            if (
                b.api.enabled
                and {"update", "delete"}.intersection(b.api.operations)
                and b.version_column is None
            ):
                diagnostics.append(
                    Diagnostic(
                        "warning",
                        "api.unversioned_writes",
                        f"{b.route_key}: configure version_id_col to detect stale writes.",
                    )
                )
            if b.policy is None or b.fields is None:
                raise ImproperlyConfigured("The compiled builder is missing policy or fields.")
            diagnostics.append(
                Diagnostic(
                    "info",
                    "resource.exposure",
                    b.route_key,
                    {
                        "fields": b.fields,
                        "label_field": b.label_field,
                        "read_only_fields": b.read_only_fields,
                        "api_read": b.api_read if b.api.enabled else (),
                        "api_write": b.api_write if b.api.enabled else (),
                        "form": tuple(key for key in b.form_fields if key not in b.read_only_fields)
                        if b.form.enabled
                        else (),
                        "api_operations": b.api.operations if b.api.enabled else (),
                        "view": b.view_list_display if b.view.enabled else (),
                        "related": b.view_related if b.view.enabled else (),
                        "conditional": type(b.policy).read_fields is not Policy.read_fields
                        or b.conditional_writes,
                    },
                )
            )
            for name in b.view_related:
                import sqlalchemy as sa

                relationship = sa.inspect(item.model).relationships[name]
                if relationship.mapper.class_ not in self._registrations:
                    diagnostics.append(
                        Diagnostic(
                            "error",
                            "relationship.unregistered_collection",
                            f"{item.slug}.{name}: register the collection's target resource.",
                        )
                    )
            for name in b.api.write_relationships:
                relation = inspect(item.model).relationships[name]
                registration = self._registrations.get(relation.mapper.class_)
                if (
                    registration is None
                    or not registration.builder.api.enabled
                    or not {"retrieve", "update"}.issubset(registration.builder.api.operations)
                ):
                    diagnostics.append(
                        Diagnostic(
                            "error",
                            "relationship.unwritable_target",
                            f"{item.slug}.{name}: register a target API with retrieve and update.",
                        )
                    )
            if b.view.enabled:
                for name in b.view_list_relations:
                    target = self._for_table(b.list_relations[name].target)
                    if target is None:
                        diagnostics.append(
                            Diagnostic(
                                "error",
                                "relationship.unregistered_display",
                                f"{item.slug}.{name}: register the relation's target resource.",
                            )
                        )
                    elif target.label_field is None:
                        diagnostics.append(
                            Diagnostic(
                                "warning",
                                "relationship.raw_label",
                                f"{item.slug}.{name}: set label_field on the target; "
                                "falling back to the raw key.",
                            )
                        )
            diagnostics.extend(
                Diagnostic(
                    "warning",
                    "relationship.unregistered",
                    f"{item.slug}.{key}: selection is blocked; register its target.",
                )
                for key in b.fields
                for fk in b.specs[key].column.foreign_keys
                if self._for_table(fk.column.table) is None and key not in b.read_only_fields
            )
        if strict and any(d.level == "error" for d in diagnostics):
            raise ImproperlyConfigured(
                "; ".join(d.message for d in diagnostics if d.level == "error")
            )
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
                session.info["obox.context"] = ctx

                def before_commit(_: Any) -> None:
                    if not committing and not session.in_nested_transaction():
                        raise ImproperlyConfigured("Only the unit of work may commit.")

                event.listen(session.sync_session, "before_commit", before_commit)
                try:
                    async with session.begin():
                        ctx._transaction = session.sync_session.get_transaction()
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
        if ctx is not None:
            for domain_event in ctx._events:
                try:
                    await invoke(
                        self.get_builder(domain_event.model).after_commit, domain_event, ctx
                    )
                except Exception:
                    logger.exception("Best-effort after_commit callback failed.")

    def context_dependency(self, source: str) -> Callable[..., Any]:
        resolver = self.settings.principal_dependency or (lambda: Principal())

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
