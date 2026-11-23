"""Authenticated contexts for custom FastAPI endpoints."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, cast

from fastapi import Depends, Request
from fastapi.routing import RouteContext, iter_route_contexts
from starlette.routing import BaseRoute

from obox.core.exceptions import ImproperlyConfigured, NotAuthenticated, PermissionDenied
from obox.core.principal import Identity, _valid_permission, has_permission
from obox.runtime.context import Context
from obox.web.identity import _identify


async def _api_context() -> Context:
    raise ImproperlyConfigured("Install Obox before using an API context dependency.")


@dataclass(frozen=True)
class _APIContext:
    permission: str | None
    csrf: bool = False

    async def __call__(
        self, request: Request, ctx: Annotated[Context, Depends(_api_context, scope="function")]
    ) -> Context:
        principal = ctx.principal
        _require_permission(principal, self.permission)
        if (
            self.csrf
            and request.method not in {"GET", "HEAD", "OPTIONS", "TRACE"}
            and getattr(request.state, "obox_bearer_principal", None) is not principal
        ):
            request.app.state.obox.csrf.validate(request, principal)
        return ctx


def api_context(*, permission: str | None = None, csrf: bool = False) -> _APIContext:
    """Require an active identity and optionally a permission, returning its API context."""
    if permission is not None and (
        not isinstance(permission, str) or not _valid_permission(permission)
    ):
        raise ImproperlyConfigured(
            "Use a permission such as app.view_model or app.resource.action."
        )
    return _APIContext(permission, csrf)


def _api_contexts(
    routes: Sequence[BaseRoute | RouteContext],
) -> Iterator[_APIContext | _RoutePermission]:
    for route in iter_route_contexts(routes):
        pending = [getattr(route, "dependant", None)]  # codespell:ignore dependant
        while pending:
            dependency = pending.pop()
            if dependency is None:
                continue
            if isinstance(dependency.call, (_APIContext, _RoutePermission)):
                yield dependency.call
            pending.extend(dependency.dependencies)


@dataclass(frozen=True)
class _RoutePermission:
    permission: str
    html: bool
    csrf: bool = True

    async def __call__(self, request: Request, actor: Any = Depends(_identify)) -> Identity:
        if self.html:
            request.state.obox_html_permission = True
        _require_permission(actor, self.permission)
        if (
            self.csrf
            and request.method not in {"GET", "HEAD", "OPTIONS", "TRACE"}
            and getattr(request.state, "obox_bearer_principal", None) is not actor
        ):
            if self.html:
                from obox.middleware.csrf import _check

                await _check(request, multipart=True)
                request.state._obox_html_csrf = True
            else:
                request.app.state.obox.csrf.validate(request, actor)
        return cast(Identity, actor)


def permission_required(permission: str, *, html: bool = False) -> _RoutePermission:
    """Require an admin-assignable permission on a native FastAPI route."""
    if not isinstance(permission, str) or not _valid_permission(permission):
        raise ImproperlyConfigured("Use a permission such as app.view_model.")
    return _RoutePermission(permission, html)


def _require_permission(actor: Any, permission: str | None) -> None:
    if (
        getattr(actor, "is_authenticated", False) is not True
        or getattr(actor, "is_active", False) is not True
    ):
        raise NotAuthenticated("Authentication is required.")
    if permission is not None and not has_permission(actor, permission):
        raise PermissionDenied("This operation is not permitted.")
