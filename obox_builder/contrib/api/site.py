"""Explicit REST publication, independent of the HTML admin."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from obox_builder.exceptions import ImproperlyConfigured
from obox_builder.resources import Resource, ResourceRegistry, get_registry


class ModelRestApi:
    """Transport customization may narrow operations and schemas, never policy."""

    exclude_operations: tuple[str, ...] = ()
    input_schemas: dict[str, type[BaseModel]] = {}
    output_schemas: dict[str, type[BaseModel]] = {}
    methods: dict[str, str] = {}
    etag_operations: tuple[str, ...] = ("update", "delete", "bulkUpdate", "deleteSelected")


@dataclass(frozen=True)
class ApiBinding:
    resource: type[Resource]
    path: str
    adapter: type[ModelRestApi]


class Api:
    def __init__(
        self,
        name: str = "default",
        *,
        registry: ResourceRegistry | None = None,
        rate_limiter: Any = None,
    ) -> None:
        self.name = name
        self._registry = registry
        self.rate_limiter = rate_limiter
        self.bindings: dict[str, ApiBinding] = {}

    @property
    def registry(self) -> ResourceRegistry:
        return self._registry if self._registry is not None else get_registry()

    def register(
        self,
        resource: type[Resource],
        *,
        path: str | None = None,
        api_class: type[ModelRestApi] = ModelRestApi,
    ) -> None:
        if not isinstance(resource, type) or not issubclass(resource, Resource):
            raise ImproperlyConfigured("Api.register expects a Resource class.")
        if not issubclass(api_class, ModelRestApi):
            raise ImproperlyConfigured("API adapters must extend ModelRestApi.")
        resource_name = resource().name
        path = path if path is not None else "/" + resource_name.replace(".", "/")
        if not re.fullmatch(r"/[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*", path):
            raise ImproperlyConfigured(f"Invalid resource API path {path!r}.")
        if path in {
            "/auth/login",
            "/auth/me",
            "/auth/logout",
            "/auth/password",
            "/tokens",
            "/uploads",
        }:
            raise ImproperlyConfigured(
                f"API path {path!r} is reserved for credential or upload endpoints."
            )
        binding = ApiBinding(resource, path, api_class)
        previous = self.bindings.get(resource_name)
        if previous is not None and previous != binding:
            raise ImproperlyConfigured(f"Resource {resource.name!r} already has an API binding.")
        if any(value.path == path and value != binding for value in self.bindings.values()):
            raise ImproperlyConfigured(f"Duplicate API resource path {path!r}.")
        self.bindings[resource_name] = binding

    def get_router(self) -> APIRouter:
        from obox_builder.contrib.api.routing import build_router

        return build_router(self)

    def reset(self) -> None:
        self.bindings.clear()


api = Api()
