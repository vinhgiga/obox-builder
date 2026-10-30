"""Resource definitions and the single authorized operation dispatcher."""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import replace
from functools import cached_property
from types import MappingProxyType
from typing import Any, ClassVar

from pydantic import BaseModel, ValidationError

from obox_builder.exceptions import ImproperlyConfigured, PermissionDenied
from obox_builder.resources.context import OperationContext
from obox_builder.resources.errors import ResourceValidationError
from obox_builder.resources.operations import Capability, Operation, OperationSpec
from obox_builder.security import PermissionRef
from obox_builder.utils.strings import humanize


class ResourcePolicy:
    """Additional constraints; grant and credential checks cannot be overridden."""

    def scope(self, ctx: OperationContext, query: Any, operation: OperationSpec) -> Any:
        return query

    def allows(self, ctx: OperationContext, operation: OperationSpec, obj: Any = None) -> bool:
        return True


def validation_errors(exc: ValidationError) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for error in exc.errors(include_input=False, include_context=False):
        key = ".".join(str(part) for part in error["loc"]) or "__all__"
        result.setdefault(key, []).append(error["msg"])
    return result


class Resource:
    __resource_definition__: ClassVar[bool] = False
    name: str = ""
    label: ClassVar[str] = ""
    exclude_operations: tuple[str, ...] = ()
    policy: ClassVar[ResourcePolicy] = ResourcePolicy()

    def __init_subclass__(cls, **kwargs: Any) -> None:
        # A plain override retains the parent's scope and authorization metadata.
        for key, value in tuple(vars(cls).items()):
            if not inspect.isfunction(value):
                continue
            inherited = next(
                (vars(base)[key] for base in cls.__mro__[1:] if key in vars(base)), None
            )
            if isinstance(inherited, Operation):
                setattr(cls, key, inherited.override(value))
        super().__init_subclass__(**kwargs)

    def __init__(self) -> None:
        parts = self.name.split(".")
        if len(parts) != 2:
            raise ImproperlyConfigured("Resource.name must be an explicit service.resource name.")
        PermissionRef(parts[0], parts[1], "read")

    def __setattr__(self, name: str, value: Any) -> None:
        if self.__dict__.get("_frozen", False):
            raise ImproperlyConfigured("Resource definitions are frozen for this runtime.")
        object.__setattr__(self, name, value)

    def freeze(self) -> None:
        for name in ("name", "label", "exclude_operations", "policy"):
            object.__setattr__(self, name, getattr(self, name))
        self.validate()
        self._frozen = True

    def bind_registry(self, registry: Any) -> None:
        """Resolve references after every resource has been instantiated."""

    @property
    def title(self) -> str:
        return self.label or humanize(self.name.rsplit(".", 1)[-1])

    @property
    def plural_name(self) -> str:
        title = self.title
        if title.endswith(("s", "x", "z", "ch", "sh")):
            return title + "es"
        return title[:-1] + "ies" if title.endswith("y") else title + "s"

    @property
    def scope_name(self) -> str:
        return self.name

    @cached_property
    def definitions(self) -> Mapping[str, Operation]:
        values: dict[str, Any] = {}
        for base in reversed(type(self).__mro__):
            values.update(vars(base))
        definitions: dict[str, Operation] = {}
        for value in values.values():
            if not isinstance(value, Operation):
                continue
            if value.key in definitions and definitions[value.key] is not value:
                raise ImproperlyConfigured(f"Duplicate operation {self.name}.{value.key}.")
            definitions[value.key] = value
        unknown = set(self.exclude_operations) - definitions.keys()
        if unknown:
            raise ImproperlyConfigured(f"Unknown excluded operations: {sorted(unknown)}.")
        return MappingProxyType(
            {key: value for key, value in definitions.items() if key not in self.exclude_operations}
        )

    @cached_property
    def operations(self) -> Mapping[str, OperationSpec]:
        return MappingProxyType(
            {key: value.compile(self) for key, value in self.definitions.items()}
        )

    @cached_property
    def permissions(self) -> Mapping[str, PermissionRef]:
        permissions: dict[str, PermissionRef] = {}
        for base in reversed(type(self).__mro__):
            for value in vars(base).values():
                if isinstance(value, Capability):
                    reference = value.bind(self.name)
                    permissions[reference.name] = reference
        for spec in self.operations.values():
            previous = permissions.get(spec.permission.name)
            if previous and previous.label != spec.permission.label:
                raise ImproperlyConfigured(
                    f"Conflicting labels for {spec.permission.name!r}; reference one capability."
                )
            permissions[spec.permission.name] = spec.permission
        return MappingProxyType(permissions)

    def validate(self) -> None:
        if inspect.iscoroutinefunction(self.policy.scope) or inspect.iscoroutinefunction(
            self.policy.allows
        ):
            raise ImproperlyConfigured(
                "Policy hooks must synchronously inspect context/query/rows."
            )
        _ = self.permissions
        for spec in self.operations.values():
            for schema, output in (
                (spec.input_schema, False),
                (spec.output_schema, True),
            ):
                if schema is None:
                    continue
                if schema is BaseModel:
                    raise ImproperlyConfigured(f"{self.name}.{spec.name} needs a concrete schema.")
                try:
                    schema.model_json_schema(mode="serialization" if output else "validation")
                except Exception as exc:
                    raise ImproperlyConfigured(
                        f"Invalid schema for {self.name}.{spec.name}: {exc}"
                    ) from exc

    def builtin_schemas(self, method: str) -> tuple[type[BaseModel] | None, type[BaseModel] | None]:
        raise ImproperlyConfigured(f"{self.name} has no builtin operation {method!r}.")

    def can_execute(self, ctx: OperationContext, operation: str, obj: Any = None) -> bool:
        spec = self.operations.get(operation)
        if spec is None:
            return False
        context = ctx.for_resource(self)
        allowed = self.policy.allows(context, spec, obj)
        if inspect.isawaitable(allowed):
            raise ImproperlyConfigured("Policy.allows must return a boolean, not an awaitable.")
        return context.can(spec.permission, effect=spec.effect) and bool(allowed)

    async def resolve_target(self, ctx: OperationContext, spec: OperationSpec, target: Any) -> Any:
        if target is None:
            raise ResourceValidationError({"target": ["A target is required."]})
        return target

    async def execute(
        self,
        operation: str,
        ctx: OperationContext,
        data: Any = None,
        *,
        target: Any = None,
    ) -> Any:
        if ctx.registry is None:
            from obox_builder.resources.registry import get_registry

            with suppress(ImproperlyConfigured):
                ctx = replace(ctx, registry=get_registry())
        if ctx.registry is not None:
            registered = ctx.registry.get(self.name)
            if type(registered) is not type(self):
                raise ImproperlyConfigured(
                    "Operation resource does not match its runtime registry."
                )
            if registered is not self:
                return await registered.execute(operation, ctx, data, target=target)
        spec = self.operations.get(operation)
        if spec is None:
            raise LookupError(f"Operation {self.name}.{operation} is unavailable.")
        context = ctx.for_resource(self)
        payload: BaseModel | None = None
        if spec.input_schema is not None:
            try:
                raw = data.model_dump(exclude_unset=True) if isinstance(data, BaseModel) else data
                payload = spec.input_schema.model_validate(
                    {} if raw is None else raw, extra="forbid", by_name=True
                )
            except ValidationError as exc:
                raise ResourceValidationError(validation_errors(exc)) from exc
        elif data not in (None, {}):
            raise ResourceValidationError({"__all__": ["This operation accepts no input."]})
        if not context.can(spec.permission, effect=spec.effect):
            raise PermissionDenied(f"Permission {spec.permission.name!r} is required.")
        kwargs: dict[str, Any] = {"ctx": context}
        if "data" in inspect.signature(self.definitions[operation].handler).parameters:
            kwargs["data"] = payload
        if spec.scope != "collection":
            resolved = await self.resolve_target(context, spec, target)
            objects = resolved if spec.scope == "selection" else [resolved]
            if not all(self.can_execute(context, operation, obj) for obj in objects):
                raise PermissionDenied("A selected record does not allow this operation.")
            kwargs["objects" if spec.scope == "selection" else "obj"] = resolved
            if spec.scope == "record":
                context = replace(context, obj=resolved)
                kwargs["ctx"] = context
        elif not self.can_execute(context, operation):
            raise PermissionDenied("This operation is not allowed.")
        result = await self.definitions[operation].handler(self, **kwargs)
        if spec.output_schema is None:
            if result is not None:
                raise ImproperlyConfigured(f"{self.name}.{operation} must return None.")
            return None
        raw = result.model_dump(exclude_unset=True) if isinstance(result, BaseModel) else result
        return spec.output_schema.model_validate(raw, by_name=True)


def register() -> Any:
    """Mark a definition for discovery without mutating a process-global registry."""

    def decorate(resource: type[Resource]) -> type[Resource]:
        if not isinstance(resource, type) or not issubclass(resource, Resource):
            raise ImproperlyConfigured("Only Resource subclasses can be registered.")
        resource.__resource_definition__ = True
        return resource

    return decorate
