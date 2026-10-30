"""Transport-independent identities and permission references."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

Effect = Literal["read", "write", "delete"]
SERVICE_PATTERN = r"[a-z][a-z0-9]*"
COMPONENT_PATTERN = r"[a-z][A-Za-z0-9]*"


def immutable(value: Any) -> Any:
    """Snapshot JSON-like identity data without retaining mutable containers."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: immutable(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(immutable(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(immutable(item) for item in value)
    return value


def lower_camel(value: str) -> str:
    if not re.fullmatch(r"[a-z][A-Za-z0-9]*(?:_[a-z0-9]+)*", value):
        raise ValueError(f"Invalid permission identifier {value!r}.")
    first, *parts = value.split("_")
    return first + "".join(part[0].upper() + part[1:] for part in parts)


@dataclass(frozen=True, slots=True)
class PermissionRef:
    service: str
    resource: str
    action: str
    label: str = field(default="", compare=False)
    effect: Effect = field(default="read", compare=False)

    def __post_init__(self) -> None:
        if not re.fullmatch(SERVICE_PATTERN, self.service) or any(
            not re.fullmatch(COMPONENT_PATTERN, value) for value in (self.resource, self.action)
        ):
            raise ValueError("Invalid permission service, resource or action.")
        if self.effect not in {"read", "write", "delete"}:
            raise ValueError(f"Invalid permission effect {self.effect!r}.")

    @property
    def name(self) -> str:
        return f"{self.service}.{self.resource}.{self.action}"

    @classmethod
    def parse(cls, name: str) -> PermissionRef:
        parts = name.split(".")
        if len(parts) != 3:
            raise ValueError("Permissions use service.resource.action syntax.")
        return cls(parts[0], parts[1], parts[2])


@dataclass(frozen=True, slots=True)
class Actor:
    """A resolved snapshot; superuser grants are expanded by the identity provider."""

    id: int | None = None
    roles: frozenset[str] = frozenset()
    permissions: frozenset[str] = frozenset()
    attributes: Mapping[str, Any] = field(default_factory=dict)
    is_superuser: bool = False
    is_active: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "roles", frozenset(self.roles))
        object.__setattr__(self, "permissions", frozenset(self.permissions))
        object.__setattr__(self, "attributes", immutable(self.attributes))

    def has_permission(self, permission: PermissionRef | str) -> bool:
        name = permission.name if isinstance(permission, PermissionRef) else permission
        return self.is_active and name in self.permissions

    @property
    def username(self) -> str:
        return str(self.attributes.get("username", ""))

    def has_perm(self, permission: PermissionRef | str) -> bool:
        return self.has_permission(permission)


def scope_allows(scopes: Sequence[str], resource: str, effect: str) -> bool:
    """Credential scopes restrict grants; they never supply missing grants."""
    for scope in scopes:
        target, _, action = scope.partition(":")
        if action in {effect, "*"} and (
            target in {"*", resource, resource.partition(".")[0] + ".*"}
        ):
            return True
    return False


__all__ = [
    "COMPONENT_PATTERN",
    "SERVICE_PATTERN",
    "Actor",
    "Effect",
    "PermissionRef",
    "lower_camel",
    "scope_allows",
]
