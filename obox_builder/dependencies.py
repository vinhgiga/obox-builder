"""Authenticated contexts for custom FastAPI endpoints."""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends
from fastapi.routing import RouteContext, iter_route_contexts
from starlette.routing import BaseRoute

from obox_builder.auth import _valid_permission, has_permission
from obox_builder.context import Context
from obox_builder.errors import ImproperlyConfigured, NotAuthenticated, PermissionDenied


async def _api_context() -> Context:
    raise ImproperlyConfigured("Install Obox before using api_context.")


@dataclass(frozen=True)
class _APIContext:
    permission: str | None

    async def __call__(
        self, ctx: Annotated[Context, Depends(_api_context, scope="function")]
    ) -> Context:
        principal = ctx.principal
        if (
            getattr(principal, "is_authenticated", False) is not True
            or getattr(principal, "is_active", False) is not True
        ):
            raise NotAuthenticated("Authentication is required.")
        if self.permission is not None and not has_permission(principal, self.permission):
            raise PermissionDenied("This operation is not permitted.")
        return ctx


def api_context(*, permission: str | None = None) -> _APIContext:
    """Require an active identity and optionally a permission, returning its API context."""
    if permission is not None and (
        not isinstance(permission, str) or not _valid_permission(permission)
    ):
        raise ImproperlyConfigured("API permissions require app.resource.action.")
    return _APIContext(permission)


def _api_contexts(routes: Sequence[BaseRoute | RouteContext]) -> Iterator[_APIContext]:
    for route in iter_route_contexts(routes):
        pending = [getattr(route, "dependant", None)]  # codespell:ignore dependant
        while pending:
            dependency = pending.pop()
            if dependency is None:
                continue
            if isinstance(dependency.call, _APIContext):
                yield dependency.call
            pending.extend(dependency.dependencies)
