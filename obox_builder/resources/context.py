"""Execution context shared by Python callers and transport adapters."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession

from obox_builder.exceptions import ImproperlyConfigured
from obox_builder.fields import RecordContext
from obox_builder.security import Actor, Effect, PermissionRef, immutable, scope_allows

if TYPE_CHECKING:
    from obox_builder.resources.base import Resource
    from obox_builder.resources.registry import ResourceRegistry


@dataclass(frozen=True)
class OperationContext:
    actor: Actor
    db: AsyncSession | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    scopes: tuple[str, ...] | None = None
    registry: ResourceRegistry | None = None
    resource: Resource | None = None
    obj: Any = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", immutable(self.metadata))
        if self.scopes is not None:
            object.__setattr__(self, "scopes", tuple(self.scopes))

    @property
    def session(self) -> AsyncSession:
        if self.db is None:
            raise ImproperlyConfigured("This operation requires a database session.")
        return self.db

    def for_resource(self, resource: Resource) -> OperationContext:
        return replace(self, resource=resource)

    @property
    def user(self) -> Actor:
        return self.actor

    def can(self, permission: PermissionRef, *, effect: Effect | None = None) -> bool:
        if self.registry is not None and permission.name not in self.registry.permissions:
            return False
        if not self.actor.has_permission(permission):
            return False
        key = (
            self.resource.scope_name
            if self.resource
            else (f"{permission.service}.{permission.resource}")
        )
        return self.scopes is None or scope_allows(self.scopes, key, effect or permission.effect)

    def delegation_actor(self) -> Actor:
        if self.scopes is None:
            return self.actor
        if self.registry is None:
            raise ImproperlyConfigured("Delegating credentials requires a resource catalog.")
        effects: dict[str, set[tuple[str, Effect]]] = {}
        for resource in self.registry.resources.values():
            for permission in resource.permissions.values():
                effects.setdefault(permission.name, set()).add(
                    (resource.scope_name, permission.effect)
                )
            for spec in resource.operations.values():
                effects.setdefault(spec.permission.name, set()).add(
                    (resource.scope_name, spec.effect)
                )
        permissions = frozenset(
            name
            for name in self.actor.permissions
            if name in effects
            and all(scope_allows(self.scopes, key, effect) for key, effect in effects[name])
        )
        return replace(self.actor, permissions=permissions, is_superuser=False)

    def record_context(
        self, obj: Any = None, values: Mapping[str, Any] | None = None
    ) -> RecordContext:
        return RecordContext(
            obj=self.obj if obj is None else obj,
            values=values or {},
            user=self.actor,
            extra=self.metadata,
        )
