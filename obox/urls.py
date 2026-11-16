"""A small path/include adapter for native FastAPI routes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import import_module
from typing import Any

from fastapi import FastAPI, Request
from starlette.convertors import (  # codespell:ignore convertors
    StringConvertor,
    register_url_convertor,
)

from obox._async import invoke


class _SlugConvertor(StringConvertor):
    regex = "[-a-zA-Z0-9_]+"


register_url_convertor("slug", _SlugConvertor())


@dataclass(frozen=True)
class URLPattern:
    route: str
    callback: Any
    default_args: dict[str, Any]
    name: str | None = None


@dataclass(frozen=True)
class URLResolver:
    patterns: Any
    app_name: str | None = None
    namespace: str | None = None


def include(arg: Any, namespace: str | None = None) -> URLResolver:
    if isinstance(arg, str):
        arg = import_module(arg)
    app_name = getattr(arg, "app_name", None)
    if isinstance(arg, tuple):
        arg, app_name = arg
    return URLResolver(getattr(arg, "urlpatterns", arg), app_name, namespace or app_name)


def path(
    route: str, view: Any, kwargs: dict[str, Any] | None = None, name: str | None = None
) -> URLPattern:
    if isinstance(view, tuple) and len(view) == 3:
        view = URLResolver(*view)
    return URLPattern(route, view, kwargs or {}, name)


def _mount(
    app: FastAPI,
    patterns: Any,
    prefix: str = "",
    namespace: str = "",
    defaults: dict[str, Any] | None = None,
) -> None:
    for pattern in patterns:
        route = prefix + pattern.route
        callback = pattern.callback
        extra = {**(defaults or {}), **pattern.default_args}
        if isinstance(callback, URLResolver):
            child_namespace = (
                namespace + callback.namespace + ":" if callback.namespace else namespace
            )
            _mount(app, callback.patterns, route, child_namespace, extra)
            continue
        converters = {"int": "int", "str": "str", "slug": "slug", "uuid": "uuid", "path": "path"}

        def replace(match: re.Match[str], converters: dict[str, str] = converters) -> str:
            converter, name = match.groups()
            if converter is not None and converter not in converters:
                raise ValueError(f"Invalid path converter: {converter!r}.")
            return "{" + name + ":" + converters[converter or "str"] + "}"

        url = "/" + re.sub(r"<(?:(\w+):)?(\w+)>", replace, route).lstrip("/")
        methods = getattr(callback, "http_method_names", ("get",))
        endpoint = callback
        if extra:
            endpoint = _with_defaults(callback, extra)
        documented = getattr(callback, "include_in_schema", False)
        groups = [[method] for method in methods] if documented else [methods]
        for group in groups:
            action = getattr(callback, "actions", {}).get(group[0])
            status = 201 if action == "create" else 204 if action == "destroy" else 200
            app.add_api_route(
                url,
                endpoint,
                methods=[method.upper() for method in group],
                name=namespace + pattern.name if pattern.name else None,
                response_model=None,
                include_in_schema=documented,
                status_code=status,
            )
        # Dispatch unsupported methods through APIView so DRF errors and permissions apply.
        extra_methods = [
            method.upper()
            for method in getattr(getattr(callback, "view_class", None), "http_method_names", ())
            if method not in methods
        ]
        if documented and extra_methods:
            app.add_api_route(
                url, endpoint, methods=extra_methods, response_model=None, include_in_schema=False
            )


def _with_defaults(callback: Any, values: dict[str, Any]) -> Any:
    async def endpoint(request: Request) -> Any:
        request.scope["path_params"] = {**request.path_params, **values}
        return await invoke(callback, request)

    return endpoint
