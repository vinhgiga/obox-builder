"""FastAPI integration, builder registration and transaction ownership."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, replace
from inspect import isawaitable, iscoroutinefunction
from types import MappingProxyType
from typing import Any, TypeVar, cast

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import RouteContext, iter_route_contexts
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import BaseRoute, Mount
from starlette.staticfiles import StaticFiles

from obox._auth import Permission, Principal
from obox._catalog import CatalogConfig
from obox._component_service import ComponentService
from obox._components import Use
from obox._config import RuntimeConfig
from obox._context import Context
from obox._database import Database
from obox._dependencies import _api_context, _api_contexts
from obox._exceptions import BuilderValidationError, OboxError, ResourceConflict
from obox._i18n import LANGUAGES, LanguageMiddleware
from obox._pages import Page
from obox._staticfiles import AssetMount
from obox.core.exceptions import ImproperlyConfigured

T = TypeVar("T")
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Diagnostic:
    level: str
    code: str
    message: str
    exposure: Mapping[str, Any] | None = None


def _overlap(left: str, right: str) -> bool:
    a, b = left.strip("/").split("/"), right.strip("/").split("/")
    return len(a) == len(b) and all(
        x == y or x.startswith("{") or y.startswith("{") for x, y in zip(a, b, strict=True)
    )


class _Runtime:
    def __init__(
        self,
        app: FastAPI,
        *,
        settings: RuntimeConfig | None = None,
        features: Sequence[Any] = (),
        defer: bool = False,
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
        self._apps: dict[str, CatalogConfig] = {}
        self._pages: dict[tuple[str, str], Page] = {}
        self._components: dict[tuple[str, str], ComponentService] = {}
        self._page_components: dict[tuple[str, str], tuple[tuple[ComponentService, ...], ...]] = {}
        self._installation_failed = False
        self.ui_prefixes = [self.settings.ui_prefix] if self.settings.ui_prefix else []
        self._ui_routes: list[BaseRoute | RouteContext] = []
        self._owned_routes: set[int] = set()
        self._frozen = False
        self.csrf = CSRF(self.settings.csrf_secret)
        self.templates = create_templates(self.settings)
        app.dependency_overrides.setdefault(_api_context, self.context_dependency("api"))
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
        self.mount_static(
            f"{self.settings.ui_prefix}/_static",
            namespace="builder",
            name="obox_static",
        )
        app.state.obox = self
        for plugin in features:
            self._activate(plugin)
        if defer:
            return
        try:
            self._load_apps()
            self._loading_apps = False
            self._finish_apps()
        except BaseException:
            self._installation_failed = True
            raise

    @property
    def apps(self) -> Mapping[str, CatalogConfig]:
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
    def pages(self) -> Mapping[tuple[str, str], Page]:
        return MappingProxyType(self._pages)

    @property
    def permissions(self) -> Mapping[str, Permission]:
        result = {}
        from obox.contrib._models import installed_models
        from obox.views.generic.detail import _model_label

        for model in installed_models():
            label, model_name = _model_label(model)
            for action in ("add", "change", "delete", "view", "comment"):
                name = f"{label}.{action}_{model_name}"
                result[name] = Permission(name, action.title(), "model")
        from obox.builder import site

        for admin in site._registry.values():
            for action in ("add", "change", "delete", "view"):
                name = f"{admin.app_label}.{action}_{admin.model_name}"
                result[name] = Permission(name, action.title(), "model")
        for page in self._pages.values():
            if page.permission is not None:
                result.setdefault(
                    page.permission,
                    Permission(page.permission, page.title or page.name, "page"),
                )
        bindings = [
            *self._components.values(),
            *(
                service
                for rows in self._page_components.values()
                for row in rows
                for service in row
            ),
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

    def add_page(self, page: Page, *, app_label: str) -> None:
        from obox._page_routes import build_page

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
            template=page.template
            or ("obox/components.html" if page.components else f"{app_label}/{page.name}.html"),
            title=page.title or page.name.replace("_", " ").title(),
            permission=f"{app_label}.pages.{page.permission}"
            if page.permission is not None and "." not in page.permission
            else page.permission,
        )
        rows = []
        keys: set[str] = set()
        for row in bound.components:
            bindings = []
            for item in row:
                placement = item if isinstance(item, Use) else None
                declaration = item.component if isinstance(item, Use) else item
                key = (
                    placement.key
                    if placement
                    else getattr(declaration, "name", None) or f"component{len(keys) + 1}"
                )
                if key in keys:
                    raise ImproperlyConfigured(
                        "Page component keys must be unique; use explicit Use keys."
                    )
                keys.add(key)
                service = ComponentService.bind(
                    self,
                    declaration,
                    app_label=app_label,
                    name=key,
                    page=bound,
                    title=placement.title if placement else None,
                    filters=placement.filters if placement else None,
                )
                if not service.definition.template and service.definition.plugin != "analytics":
                    raise ImproperlyConfigured(
                        "Page components require a template or a supported renderer."
                    )
                bindings.append(service)
            rows.append(tuple(bindings))
        self._page_components[app_label, bound.name] = tuple(rows)
        router = build_page(self, bound, app_label)
        for candidate in iter_route_contexts(router.routes):
            if any(
                self._conflict(candidate, other) for other in iter_route_contexts(self.app.routes)
            ):
                raise ImproperlyConfigured(f"Page route conflicts at {candidate.path!r}.")
        self.include_router(router, ui=True)
        self._pages[app_label, page.name] = bound
        self.add_ui_prefix(config.ui_path(self.settings))

    def add_app(self, config: CatalogConfig) -> None:
        self._check_mutable()
        if not self._loading_apps:
            raise ImproperlyConfigured("Declare applications before app configuration is complete.")
        config = CatalogConfig.load(config)
        if config.label in self._apps or any(c.name == config.name for c in self._apps.values()):
            raise ImproperlyConfigured(f"Application {config.label!r} is already installed.")
        config.ui_path(self.settings)
        self._apps[config.label] = config

    def _load_apps(self) -> None:

        for entry in self.settings.installed_apps:
            self.add_app(CatalogConfig.load(entry))
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
                self.include_router(build_component_api(self, config.label))
            for page in config.pages:
                self.add_page(page, app_label=config.label)
            for router in config.routers:
                self.include_router(router, prefix=f"{self.settings.api_prefix}/{config.label}")
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
        self.add_ui_prefix(self.settings.ui_prefix or "/")
        if self._pages:
            self.include_router(build_catalog(self), ui=True)

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
        self._ui_routes.append(route)

    @staticmethod
    async def _problem(request: Request, exc: Exception) -> Response:
        from obox._routes import authentication_response

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

    def check(self, *, strict: bool = False) -> Sequence[Diagnostic]:
        if self._installation_failed:
            raise ImproperlyConfigured("Application setup failed.")
        from jinja2 import TemplateNotFound

        from obox.api import _check_route_conflicts

        _check_route_conflicts(iter_route_contexts(self.app.routes))

        diagnostics = []
        definitions = [service.definition for service in self._components.values()]
        names = [definition.template for definition in definitions if definition.template]
        names.extend(page.template for page in self._pages.values() if page.template)
        for name in names:
            try:
                self.templates.env.get_template(name)
            except TemplateNotFound:
                diagnostics.append(
                    Diagnostic("error", "missing_template", f"Missing template: {name}")
                )
        if strict and diagnostics:
            raise ImproperlyConfigured("; ".join(item.message for item in diagnostics))
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
