"""Shared resource contracts, authorized operations, and explicit publication."""

from obox_builder.resources.base import Resource, ResourcePolicy, register
from obox_builder.resources.context import OperationContext
from obox_builder.resources.errors import ResourceConflict, ResourceValidationError
from obox_builder.resources.models import (
    Batch,
    ModelResource,
    Page,
    PageInfo,
    QueryInput,
    ReadOnlyModelResource,
)
from obox_builder.resources.operations import OperationSpec, capability, operation
from obox_builder.resources.registry import ResourceRegistry, get_registry
from obox_builder.resources.relations import Related
from obox_builder.security import lower_camel


def permission_for_model(model, action: str) -> str:
    resource = get_registry().for_model(model)
    key = {"view": "get", "read": "get", "add": "create", "change": "update"}.get(action, action)
    spec = resource.operations.get(key)
    if spec is not None:
        return spec.permission.name
    if key in {"create", "update", "delete"} and f"{resource.name}.manage" in resource.permissions:
        return f"{resource.name}.manage"
    return f"{resource.name}.{lower_camel(action)}"


__all__ = [
    "Batch",
    "ModelResource",
    "OperationContext",
    "OperationSpec",
    "Page",
    "PageInfo",
    "QueryInput",
    "ReadOnlyModelResource",
    "Related",
    "Resource",
    "ResourceConflict",
    "ResourcePolicy",
    "ResourceRegistry",
    "ResourceValidationError",
    "capability",
    "get_registry",
    "operation",
    "permission_for_model",
    "register",
]
