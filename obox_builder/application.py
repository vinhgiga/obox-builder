"""Native FastAPI application, registration and transaction ownership."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import iter_route_contexts
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm.exc import StaleDataError
from starlette.routing import Mount

from obox_builder.auth import Policy
from obox_builder.builder import ModelBuilder
from obox_builder.builder.service import ResourceService, invoke
from obox_builder.context import Context
from obox_builder.errors import (
    BuilderValidationError,
    ImproperlyConfigured,
    OboxError,
    ResourceConflict,
)

T = TypeVar("T")
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


def _overlap(left: str, right: str) -> bool:
    a, b = left.strip("/").split("/"), right.strip("/").split("/")
    return len(a) == len(b) and all(
        x == y or x.startswith("{") or y.startswith("{") for x, y in zip(a, b, strict=True)
    )


class Obox(FastAPI):
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        principal_dependency: Callable[..., Any] | None = None,
        default_policy: Policy[Any] | None = None,
        ui_prefix: str = "/admin",
        api_prefix: str = "/api/v1",
        csrf_secret: str | bytes | None = None,
        **fastapi_kwargs: Any,
    ) -> None:
        from obox_builder.builder.csrf import CSRF

        self.session_factory = session_factory
        self.principal_dependency, self.default_policy = principal_dependency, default_policy
        self.ui_prefix, self.api_prefix = ui_prefix.rstrip("/"), api_prefix.rstrip("/")
        for prefix in (self.ui_prefix, self.api_prefix):
            if not prefix.startswith("/") or any(char in prefix for char in "{}?#"):
                raise ImproperlyConfigured("Surface prefixes must be nonempty absolute URL paths.")
        self._registrations: dict[type[Any], Registration[Any]] = {}
        self._generated: dict[Callable[..., Any] | None, type[Any]] = {}
        self._frozen = False
        self.csrf = CSRF(csrf_secret)
        user_lifespan = fastapi_kwargs.pop("lifespan", None)
        fastapi_kwargs.setdefault("version", "0.4.0")

        @asynccontextmanager
        async def lifespan(app: FastAPI) -> AsyncIterator[Any]:
            self.check(strict=True)
            self._frozen = True
            async with original_lifespan(app) as state:
                self.check(strict=True)
                yield state

        super().__init__(lifespan=user_lifespan, **fastapi_kwargs)
        original_lifespan = self.router.lifespan_context
        self.router.lifespan_context = lifespan
        self.add_exception_handler(OboxError, self._problem)
        self.add_exception_handler(RequestValidationError, self._validation_problem)

    @staticmethod
    async def _problem(request: Request, exc: Exception) -> JSONResponse:
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
        return JSONResponse(body, status_code=error.status, media_type="application/problem+json")

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

    def register(
        self, model: type[T], *, builder: type[ModelBuilder[T]] | None = None
    ) -> Registration[T]:
        from obox_builder.builder.api import build_api
        from obox_builder.builder.ui import build_ui

        if self._frozen:
            raise ImproperlyConfigured("Registration is frozen after application startup.")
        if not isinstance(model, type):
            raise ImproperlyConfigured("Register a SQLAlchemy mapped class.")
        if builder is not None and (
            not isinstance(builder, type) or not issubclass(builder, ModelBuilder)
        ):
            raise ImproperlyConfigured("builder must be a ModelBuilder subclass.")
        if not callable(self.principal_dependency) or not isinstance(self.default_policy, Policy):
            raise ImproperlyConfigured("Registration requires authentication and a default policy.")
        if model in self._registrations:
            raise ImproperlyConfigured("This model is already registered.")
        definition = (builder or ModelBuilder)()
        definition._compile(model, self.default_policy, self)
        if definition.slug is None:
            raise ImproperlyConfigured("The compiled builder has no slug.")
        if any(item.slug == definition.slug for item in self._registrations.values()):
            raise ImproperlyConfigured(f"Duplicate resource slug: {definition.slug!r}.")
        service = ResourceService[T](definition, self)
        routers = (build_api(self, definition, service), build_ui(self, definition, service))
        candidates = [route for router in routers for route in iter_route_contexts(router.routes)]
        existing = list(iter_route_contexts(self.routes))
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
        )
        self._registrations[model] = registration
        for route in candidates:
            self._generated[route.endpoint] = model
        for router in routers:
            self.include_router(router)
        self.openapi_schema = None
        return registration

    @staticmethod
    def _conflict(left: Any, right: Any, *, internal: bool = False) -> bool:
        if left.name and left.name == right.name:
            return True
        if not left.path or not right.path:
            return False
        if isinstance(right.original_route, Mount):
            return left.path == right.path or left.path.startswith(right.path.rstrip("/") + "/")
        if not set(left.methods or ()).intersection(right.methods or ()):
            return False
        if internal and left.path != right.path:
            # Generated /new precedes /{id}; both are intentional within one resource.
            return False
        return _overlap(left.path, right.path)

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

        diagnostics = []
        routes = list(iter_route_contexts(self.routes))
        for i, route in enumerate(routes):
            for other in routes[:i]:
                model = self._generated.get(route.endpoint)
                other_model = self._generated.get(other.endpoint)
                if model is None and other_model is None:
                    continue
                internal = model is not None and model is other_model
                if self._conflict(route, other, internal=internal) or (
                    isinstance(route.original_route, Mount) and self._conflict(other, route)
                ):
                    diagnostics.append(
                        Diagnostic("error", "route.conflict", f"Route conflict: {route.path}.")
                    )
        for item in self._registrations.values():
            b = item.builder
            if b.policy is None or b.fields is None:
                raise ImproperlyConfigured("The compiled builder is missing policy or fields.")
            diagnostics.append(
                Diagnostic(
                    "info",
                    "resource.exposure",
                    item.slug,
                    {
                        "fields": b.fields,
                        "read_only_fields": b.read_only_fields,
                        "api_read": b.api_read if b.api.enabled else (),
                        "api_write": b.api_write if b.api.enabled else (),
                        "form": tuple(key for key in b.form_fields if key not in b.read_only_fields)
                        if b.form.enabled
                        else (),
                        "api_actions": b.api.actions if b.api.enabled else (),
                        "view": b.view_columns if b.view.enabled else (),
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
            for key in b.fields:
                for fk in b.specs[key].column.foreign_keys:
                    if self._for_table(fk.column.table) is None and key not in b.read_only_fields:
                        diagnostics.append(
                            Diagnostic(
                                "warning",
                                "relationship.unregistered",
                                f"{item.slug}.{key}: selection is blocked; register its target.",
                            )
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
        tenant: Any = None,
        locale: str | None = None,
        trace_id: str | None = None,
    ) -> AsyncIterator[Context]:
        ctx = None
        committing = False
        try:
            async with self.session_factory(
                expire_on_commit=False, close_resets_only=False
            ) as session:
                ctx = Context(
                    principal, session, request, source, tenant, locale, trace_id, self, True
                )

                def before_commit(_: Any) -> None:
                    if not committing:
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
        async def dependency(
            request: Request, principal: Any = Depends(self.principal_dependency)
        ) -> AsyncIterator[Context]:
            async with self.uow(principal=principal, source=source, request=request) as ctx:
                yield ctx

        return dependency
