"""Operation descriptors retain authorization on direct Python calls."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, get_type_hints

from pydantic import BaseModel

from obox_builder.exceptions import ImproperlyConfigured
from obox_builder.resources.context import OperationContext
from obox_builder.security import Effect, PermissionRef, lower_camel

Scope = Literal["collection", "record", "selection"]


class Capability:
    def __init__(self, label: str, *, name: str | None = None, effect: Effect = "read") -> None:
        self.label = label
        self.name = name
        self.effect: Effect = effect
        self.attribute = ""

    def __set_name__(self, owner: type[Any], name: str) -> None:
        self.attribute = name

    def bind(self, resource_name: str) -> PermissionRef:
        service, resource = resource_name.split(".")
        return PermissionRef(
            service, resource, lower_camel(self.name or self.attribute), self.label, self.effect
        )

    def __get__(self, instance: Any, owner: type[Any]) -> PermissionRef:
        return self.bind(instance.name if instance is not None else owner.name)


def capability(label: str, *, name: str | None = None, effect: Effect = "read") -> Capability:
    return Capability(label, name=name, effect=effect)


@dataclass(frozen=True)
class OperationSpec:
    method: str
    name: str
    scope: Scope
    effect: Effect
    permission: PermissionRef
    input_schema: type[BaseModel] | None
    output_schema: type[BaseModel] | None
    builtin: bool = False


class BoundOperation:
    def __init__(self, resource: Any, descriptor: Operation) -> None:
        self.resource, self.descriptor = resource, descriptor

    @property
    def spec(self) -> OperationSpec:
        return self.resource.operations[self.descriptor.key]

    @property
    def permission(self) -> PermissionRef:
        return self.spec.permission

    async def __call__(self, ctx: Any, data: Any = None, *, target: Any = None) -> Any:
        return await self.resource.execute(self.descriptor.key, ctx, data, target=target)


class Operation:
    def __init__(
        self,
        handler: Callable[..., Any],
        *,
        name: str | None = None,
        scope: Scope = "collection",
        effect: Effect = "write",
        label: str | None = None,
        permission: str | PermissionRef | None = None,
        builtin: bool = False,
    ) -> None:
        if scope not in {"collection", "record", "selection"}:
            raise ImproperlyConfigured(f"Unknown operation scope {scope!r}.")
        if effect not in {"read", "write", "delete"}:
            raise ImproperlyConfigured(f"Unknown operation effect {effect!r}.")
        if not inspect.iscoroutinefunction(handler):
            raise ImproperlyConfigured("Operation handlers must be async methods.")
        self.handler, self.method = handler, handler.__name__
        self.key = lower_camel(self.method if name is None else name)
        self.scope: Scope = scope
        self.effect: Effect = effect
        self.label, self.permission, self.builtin = label, permission, builtin

    def __get__(self, instance: Any, owner: type[Any]) -> Any:
        return BoundOperation(instance or owner(), self)

    def override(self, handler: Callable[..., Any]) -> Operation:
        return Operation(
            handler,
            name=self.key,
            scope=self.scope,
            effect=self.effect,
            label=self.label,
            permission=self.permission,
            builtin=self.builtin,
        )

    def compile(self, resource: Any) -> OperationSpec:
        permission = self.permission
        if isinstance(permission, PermissionRef):
            reference = permission
        else:
            action = lower_camel(permission or self.key)
            service, name = resource.name.split(".")
            reference = PermissionRef(
                service, name, action, self.label or f"Can {action} {resource.title}", self.effect
            )
        if self.builtin:
            input_schema, output_schema = resource.builtin_schemas(self.method)
        else:
            try:
                hints = get_type_hints(self.handler)
            except (NameError, TypeError) as exc:
                raise ImproperlyConfigured(
                    f"Cannot resolve annotations for {resource.name}.{self.method}: {exc}"
                ) from exc
            parameters = inspect.signature(self.handler).parameters
            allowed = {"self", "ctx", "data"}
            if self.scope == "record":
                allowed.add("obj")
            elif self.scope == "selection":
                allowed.add("objects")
            scope_parameter = {"record": "obj", "selection": "objects"}.get(self.scope)
            if (
                not {"self", "ctx"}.issubset(parameters)
                or set(parameters) - allowed
                or scope_parameter is not None
                and scope_parameter not in parameters
            ):
                raise ImproperlyConfigured(
                    f"{resource.name}.{self.method} must accept ctx and its declared scope/input."
                )
            input_schema = hints.get("data") if "data" in parameters else None
            output_schema = hints.get("return")
            if "return" not in hints:
                raise ImproperlyConfigured(
                    f"{resource.name}.{self.method} needs a return annotation."
                )
            for schema in (input_schema, output_schema):
                if schema not in (None, type(None)) and (
                    not isinstance(schema, type) or not issubclass(schema, BaseModel)
                ):
                    raise ImproperlyConfigured(
                        "Operation input/output must be Pydantic models or None."
                    )
            if hints.get("ctx", OperationContext) is not OperationContext:
                raise ImproperlyConfigured("Operation handlers receive OperationContext.")
            if "data" in parameters and "data" not in hints:
                raise ImproperlyConfigured(
                    "An operation data parameter needs a Pydantic annotation."
                )
            output_schema = None if output_schema is type(None) else output_schema
            input_schema = None if input_schema is type(None) else input_schema
        return OperationSpec(
            self.method,
            self.key,
            self.scope,
            self.effect,
            reference,
            input_schema,
            output_schema,
            self.builtin,
        )


def operation(
    *,
    name: str | None = None,
    scope: Scope = "collection",
    effect: Effect = "write",
    label: str | None = None,
    permission: str | PermissionRef | None = None,
) -> Callable[[Callable[..., Any]], Operation]:
    def decorate(handler: Callable[..., Any]) -> Operation:
        return Operation(
            handler, name=name, scope=scope, effect=effect, label=label, permission=permission
        )

    return decorate
