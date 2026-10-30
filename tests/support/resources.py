"""Rebuild immutable catalogs when a test changes resource configuration."""

from obox_builder.resources import ResourceRegistry, get_registry
from obox_builder.resources import registry as registry_module


def rebuild_registry(monkeypatch):
    registry = ResourceRegistry(type(resource) for resource in get_registry().resources.values())
    monkeypatch.setattr(registry_module, "_registry", registry)
    return registry
