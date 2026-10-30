"""A fresh, frozen resource catalog for each application setup."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from importlib import import_module
from importlib.util import find_spec
from types import MappingProxyType
from typing import Any

from obox_builder.exceptions import ImproperlyConfigured
from obox_builder.resources.base import Resource
from obox_builder.security import PermissionRef


class ResourceRegistry:
    def __init__(self, resources: Iterable[type[Resource]] = ()) -> None:
        values: dict[str, Resource] = {}
        permissions: dict[str, PermissionRef] = {}
        models: dict[type[Any], Resource] = {}
        for definition in resources:
            resource = definition()
            previous = values.get(resource.name)
            if previous is not None:
                if type(previous) is definition:
                    continue
                raise ImproperlyConfigured(f"Duplicate resource {resource.name!r}.")
            values[resource.name] = resource
            model = getattr(resource, "model", None)
            if model is not None:
                if model in models:
                    raise ImproperlyConfigured(f"Multiple resources own model {model.__name__}.")
                models[model] = resource
        self.resources: Mapping[str, Resource] = MappingProxyType(values)
        self.permissions: Mapping[str, PermissionRef] = MappingProxyType(permissions)
        self.models: Mapping[type[Any], Resource] = MappingProxyType(models)
        for resource in values.values():
            resource.bind_registry(self)
        for resource in values.values():
            resource.freeze()
            for name, permission in resource.permissions.items():
                if f"{permission.service}.{permission.resource}" != resource.name:
                    continue
                if name in permissions and permissions[name].label != permission.label:
                    raise ImproperlyConfigured(f"Conflicting permission {name!r}.")
                permissions[name] = permission
        for resource in values.values():
            references = [spec.permission for spec in resource.operations.values()]
            for spec in getattr(resource, "specs", {}).values():
                references.extend(
                    ref
                    for key in ("read_permission", "write_permission")
                    if (ref := spec.options.get(key)) is not None
                )
            for reference in references:
                if reference.name not in permissions:
                    raise ImproperlyConfigured(f"Unknown permission reference {reference.name!r}.")

    def get(self, name: str) -> Resource:
        try:
            return self.resources[name]
        except KeyError:
            raise LookupError(f"Resource {name!r} is not registered.") from None

    def for_model(self, model: type[Any]) -> Any:
        try:
            return self.models[model]
        except KeyError:
            raise LookupError(f"Model {model.__name__} has no registered resource.") from None


_registry: ResourceRegistry | None = None


def discover(packages: Iterable[str]) -> ResourceRegistry:
    definitions: list[type[Resource]] = []
    for package in packages:
        path = f"{package}.resources"
        try:
            spec = find_spec(path)
        except ModuleNotFoundError as exc:
            if exc.name not in {package, path}:
                raise
            continue
        if spec is None:
            continue
        module = import_module(path)
        definitions.extend(
            value
            for value in vars(module).values()
            if isinstance(value, type)
            and issubclass(value, Resource)
            and value.__module__ == module.__name__
            and vars(value).get("__resource_definition__", False)
        )
    return ResourceRegistry(definitions)


def initialize(packages: Iterable[str]) -> ResourceRegistry:
    global _registry
    _registry = discover(packages)
    return _registry


def get_registry() -> ResourceRegistry:
    if _registry is None:
        raise ImproperlyConfigured("Resource registry is not ready; call obox_builder.setup().")
    return _registry


def reset() -> None:
    global _registry
    _registry = None
