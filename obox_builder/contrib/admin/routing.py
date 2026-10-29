"""Admin page error handling and shared custom-route registration."""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable, Coroutine, Sequence
from typing import Any, cast, get_type_hints

from fastapi import APIRouter, Depends, Request, Response
from jinja2 import TemplateNotFound

from obox_builder.contrib.admin.context import AdminContext, current_site
from obox_builder.contrib.admin.options import AdminRoute, ModelAdmin
from obox_builder.contrib.auth.routing import SessionRenewalRoute
from obox_builder.core.application import permission_denied_handler
from obox_builder.exceptions import ImproperlyConfigured, PermissionDenied
from obox_builder.templating import render


class AdminPageRoute(SessionRenewalRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                return await handler(request)
            except PermissionDenied as exc:
                configured = request.app.exception_handlers.get(PermissionDenied)
                if configured is not None and configured is not permission_denied_handler:
                    raise
                accept = request.headers.get("accept", "")
                if "text/html" in accept and "application/json" not in accept:
                    site = current_site(request)
                    try:
                        return render(
                            request,
                            "admin/errors/403.html",
                            {
                                "site_name": site.site_header,
                                "site_header": site.site_header,
                                "site_title": site.site_title,
                                "reason": str(exc),
                            },
                            status_code=403,
                        )
                    except TemplateNotFound:
                        pass
                return await permission_denied_handler(request, exc)

        return handle


def register_custom_routes(
    router: APIRouter,
    admin: ModelAdmin,
    base: str,
    *,
    context_factory: Callable[[ModelAdmin, str | None], Any],
    api: bool = False,
) -> None:
    routes = admin.get_api_urls() if api else admin.get_urls()
    names: set[str] = set()
    paths: set[str] = set()
    for custom in routes:
        if (
            not isinstance(custom, AdminRoute)
            or not callable(custom.endpoint)
            or not isinstance(custom.path, str)
            or not isinstance(custom.name, str)
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", custom.name)
            or not custom.path.strip("/")
            or "?" in custom.path
            or "#" in custom.path
            or any(part in {".", ".."} for part in custom.path.split("/"))
        ):
            raise ImproperlyConfigured("Admin routes need a safe relative path and a valid name.")
        path = custom.path.strip("/")
        if api and path.split("/", 1)[0] in {
            "schema",
            "records",
            "records:query",
            "records:delete",
            "records:upsert",
            "options",
            "actions",
            "views",
        }:
            raise ImproperlyConfigured("Custom API routes cannot replace built-in resources.")
        if any(not part for part in path.split("/")):
            raise ImproperlyConfigured("Admin route paths cannot contain empty segments.")
        if path in paths or custom.name in names:
            raise ImproperlyConfigured("Admin routes must have unique paths and names per model.")
        paths.add(path)
        names.add(custom.name)
        if isinstance(custom.methods, str) or not isinstance(custom.methods, Sequence):
            raise ImproperlyConfigured("Admin route methods must be a sequence of HTTP methods.")
        if any(not isinstance(method, str) for method in custom.methods):
            raise ImproperlyConfigured("Admin route methods must be strings.")
        methods = tuple(method.upper() for method in custom.methods)
        if not methods or any(not re.fullmatch(r"[A-Z]+", method) for method in methods):
            raise ImproperlyConfigured("Admin route methods must be non-empty HTTP method names.")
        if custom.permission is not None and not isinstance(custom.permission, str):
            raise ImproperlyConfigured("Admin route permissions must be strings or None.")
        route = custom

        admin_context = context_factory(admin, route.permission)

        endpoint = _inject_admin_context(custom.endpoint, admin_context)
        router.add_api_route(
            f"{base}/{path}",
            endpoint,
            methods=list(methods),
            name=f"{'api_' if api else ''}{admin.route_prefix}_{custom.name}",
            dependencies=[Depends(admin_context)],
        )


def _inject_admin_context(endpoint: Any, dependency: Any) -> Any:
    """Inject AdminContext into a custom view while preserving its FastAPI signature."""
    signature = inspect.signature(endpoint)
    try:
        annotations = get_type_hints(endpoint, include_extras=True)
    except (NameError, TypeError):
        annotations = {}
    parameter = signature.parameters.get("ctx")
    if parameter is None:
        return endpoint
    if parameter.kind not in {
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    }:
        raise ImproperlyConfigured(
            "A custom admin view's ctx parameter must accept a keyword value."
        )
    context_parameter = parameter.replace(
        kind=inspect.Parameter.KEYWORD_ONLY,
        default=Depends(dependency),
        annotation=AdminContext,
    )
    parameters = [
        item.replace(annotation=annotations.get(item.name, item.annotation))
        for item in signature.parameters.values()
        if item.name != "ctx"
    ]
    insert_at = next(
        (
            index
            for index, item in enumerate(parameters)
            if item.kind == inspect.Parameter.VAR_KEYWORD
        ),
        len(parameters),
    )
    parameters.insert(insert_at, context_parameter)

    async def contextual_view(**kwargs: Any) -> Any:
        result = endpoint(**kwargs)
        return await result if inspect.isawaitable(result) else result

    contextual_view.__name__ = getattr(endpoint, "__name__", "admin_view")
    contextual_view.__doc__ = getattr(endpoint, "__doc__", None)
    view_signature = signature.replace(
        parameters=parameters,
        return_annotation=annotations.get("return", signature.return_annotation),
    )
    cast(Any, contextual_view).__signature__ = view_signature
    return contextual_view
