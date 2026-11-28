"""Authenticated contexts for custom FastAPI endpoints."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Annotated, Any, ClassVar, cast

from fastapi import Depends, Request
from fastapi.routing import RouteContext, iter_route_contexts
from starlette.routing import BaseRoute

from obox.core.exceptions import ImproperlyConfigured, NotAuthenticated, PermissionDenied
from obox.core.principal import Identity, has_permission, valid_permission
from obox.runtime.context import Context
from obox.web.identity import identify

READ_METHODS = frozenset({"GET", "HEAD"})


class _Public(Enum):
    PUBLIC = "public"


PUBLIC = _Public.PUBLIC
PermissionPolicy = str | tuple[str, ...] | _Public


def permission_names(policy: PermissionPolicy, *, allow_public: bool) -> tuple[str, ...]:
    if policy is PUBLIC:
        if not allow_public:
            raise ImproperlyConfigured("PUBLIC is only supported on GET and HEAD routes.")
        return ()
    names = policy if isinstance(policy, tuple) else (policy,)
    if (
        not names
        or not all(isinstance(name, str) and valid_permission(name) for name in names)
        or len(set(names)) != len(names)
    ):
        raise ImproperlyConfigured("Use unique permissions such as app.view_model.")
    return names


async def api_context_dependency() -> Context:
    raise ImproperlyConfigured("Install Obox before using an API context dependency.")


@dataclass(frozen=True)
class _APIContext:
    permissions: tuple[str, ...]
    csrf: bool = False
    public: bool = False

    async def __call__(
        self,
        request: Request,
        ctx: Annotated[Context, Depends(api_context_dependency, scope="function")],
    ) -> Context:
        if self.public:
            if request.method not in READ_METHODS:
                raise ImproperlyConfigured("PUBLIC is only supported on GET and HEAD routes.")
            return ctx
        principal = ctx.principal
        _require_permission(principal, self.permissions)
        if (
            self.csrf
            and request.method not in {"GET", "HEAD", "OPTIONS", "TRACE"}
            and getattr(request.state, "obox_bearer_principal", None) is not principal
        ):
            request.app.state.obox.csrf.validate(request, principal)
        return ctx

    def with_context(self, context: Callable[..., Any]) -> Callable[..., Any]:
        async def authorized(
            request: Request, ctx: Context = Depends(context, scope="function")
        ) -> Context:
            return await self(request, ctx)

        authorized.__dict__["obox_api_policy"] = self
        return authorized


def api_principal_context(resolver: Callable[..., Any]) -> Callable[..., Any]:
    async def context(
        request: Request, principal: Identity = Depends(resolver)
    ) -> AsyncIterator[Context]:
        runtime = getattr(request.app.state, "obox", None)
        if runtime is None:
            raise ImproperlyConfigured("Install Obox before using an API context dependency.")
        request.scope["user"] = principal
        async with runtime.uow(
            principal=principal,
            source="api",
            request=request,
            trace_id=getattr(request.state, "trace_id", None),
        ) as ctx:
            yield ctx

    return context


def api_context(*, permission: PermissionPolicy | None = None, csrf: bool = False) -> _APIContext:
    """Return an authorized API context, or allow anonymous reads with PUBLIC."""
    names = () if permission is None else permission_names(permission, allow_public=True)
    return _APIContext(names, csrf, public=permission is PUBLIC)


def iter_api_contexts(
    routes: Sequence[BaseRoute | RouteContext],
) -> Iterator[_APIContext | _RoutePermission]:
    for route in iter_route_contexts(routes):
        pending = [getattr(route, "dependant", None)]  # codespell:ignore dependant
        while pending:
            dependency = pending.pop()
            if dependency is None:
                continue
            policy = getattr(dependency.call, "obox_api_policy", dependency.call)
            if isinstance(policy, (_APIContext, _RoutePermission)):
                yield policy
            pending.extend(dependency.dependencies)


@dataclass(frozen=True)
class _RoutePermission:
    public: ClassVar[bool] = False
    permission: str
    html: bool
    csrf: bool = True

    @property
    def permissions(self) -> tuple[str, ...]:
        return (self.permission,)

    async def __call__(self, request: Request, actor: Any = Depends(identify)) -> Identity:
        if self.html:
            request.state.obox_html_permission = True
        _require_permission(actor, self.permissions)
        if (
            self.csrf
            and request.method not in {"GET", "HEAD", "OPTIONS", "TRACE"}
            and getattr(request.state, "obox_bearer_principal", None) is not actor
        ):
            if self.html:
                from obox.middleware.csrf import check_csrf

                await check_csrf(request, multipart=True)
                request.state._obox_html_csrf = True
            else:
                request.app.state.obox.csrf.validate(request, actor)
        return cast(Identity, actor)


def permission_required(permission: str, *, html: bool = False) -> _RoutePermission:
    """Require an admin-assignable permission on a native FastAPI route."""
    if not isinstance(permission, str) or not valid_permission(permission):
        raise ImproperlyConfigured("Use a permission such as app.view_model.")
    return _RoutePermission(permission, html)


def _require_permission(actor: Any, permissions: tuple[str, ...]) -> None:
    if (
        getattr(actor, "is_authenticated", False) is not True
        or getattr(actor, "is_active", False) is not True
    ):
        raise NotAuthenticated("Authentication is required.")
    if not all(has_permission(actor, permission) for permission in permissions):
        raise PermissionDenied("This operation is not permitted.")
