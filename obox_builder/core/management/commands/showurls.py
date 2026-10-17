from __future__ import annotations

from typing import Any

from obox_builder.core.management import utils
from obox_builder.core.management.base import command


def _walk_routes(routes: list[Any], prefix: str = "") -> list[tuple[str, str]]:
    """Flatten Starlette/FastAPI route trees into (methods, path) pairs."""
    from starlette.routing import Mount

    found: list[tuple[str, str]] = []
    for route in routes:
        if isinstance(route, Mount):
            found.extend(_walk_routes(route.routes, prefix + route.path))
            continue
        methods = getattr(route, "methods", None)
        if methods is not None:
            found.append((",".join(sorted(methods)), prefix + route.path))
            continue
        original = getattr(route, "original_router", None)
        if original is not None:  # FastAPI included-router wrapper
            include_context = getattr(route, "include_context", None)
            inner_prefix = prefix + (getattr(include_context, "prefix", "") or "")
            found.extend(_walk_routes(original.routes, inner_prefix))
            continue
        subroutes = getattr(route, "routes", None)
        if subroutes is not None:
            found.extend(_walk_routes(subroutes, prefix + getattr(route, "path", "")))
    return found


@command(help="List the URL routes of the ASGI application.")
def handle() -> None:
    from obox_builder.core.application import get_asgi_application

    application = get_asgi_application()
    routes = _walk_routes(list(application.routes))
    if not routes:
        utils.echo("No routes registered.")
        return
    width = max(len(path) for _, path in routes)
    for methods, path in routes:
        utils.echo(f"{methods:<12} {path:<{width}}")
