"""Permission identifiers, immutable actors and application declarations."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from obox_builder.utils.strings import camel_to_snake

SERVICE_PATTERN = r"[a-z][a-z0-9]*"
COMPONENT_PATTERN = r"[a-z][A-Za-z0-9]*"
ACTION_NAMES = {"view": "read", "add": "create", "change": "update"}


def lower_camel(value: str) -> str:
    if not re.fullmatch(r"[a-z][A-Za-z0-9]*(?:_[a-z0-9]+)*", value):
        raise ValueError(f"Invalid permission identifier {value!r}.")
    first, *parts = value.split("_")
    return first + "".join(part[0].upper() + part[1:] for part in parts)


@dataclass(frozen=True, slots=True)
class PermissionName:
    service: str
    resource: str
    action: str

    def __post_init__(self) -> None:
        if not re.fullmatch(SERVICE_PATTERN, self.service) or any(
            not re.fullmatch(COMPONENT_PATTERN, value) for value in (self.resource, self.action)
        ):
            raise ValueError("Invalid permission service, resource or action.")

    @property
    def name(self) -> str:
        return f"{self.service}.{self.resource}.{self.action}"

    @classmethod
    def parse(cls, name: str) -> PermissionName:
        parts = name.split(".")
        if len(parts) != 3:
            raise ValueError("Permissions use service.resource.action syntax.")
        return cls(*parts)


def model_permission(model: type[Any], action: str) -> str:
    service = model._model_label.replace("_", "")
    resource = model.__permission_resource__ or lower_camel(camel_to_snake(model.__name__))
    action = ACTION_NAMES.get(action, action)
    if "manage" in model.__permission_actions__ and action in {"create", "update", "delete"}:
        action = "manage"
    return PermissionName(service, resource, lower_camel(action)).name


@dataclass(frozen=True, slots=True)
class Actor:
    id: int | None = None
    roles: frozenset[str] = frozenset()
    permissions: frozenset[str] = frozenset()
    attributes: Mapping[str, Any] = field(default_factory=dict)
    is_superuser: bool = False
    is_active: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "roles", frozenset(self.roles))
        object.__setattr__(self, "permissions", frozenset(self.permissions))
        object.__setattr__(self, "attributes", MappingProxyType(dict(self.attributes)))

    def has_permission(self, permission: str) -> bool:
        return self.is_active and (self.is_superuser or permission in self.permissions)

    def has_perm(self, permission: str) -> bool:
        return self.has_permission(permission)


class AuthorizationCatalog:
    def __init__(self) -> None:
        self.permissions: dict[str, PermissionName] = {}
        self.roles: dict[str, frozenset[str]] = {}

    def register_permission(self, service: str, resource: str, action: str) -> str:
        permission = PermissionName(service, resource, action)
        self.permissions.setdefault(permission.name, permission)
        return permission.name

    def register_role(self, name: str, *, permissions: frozenset[str] = frozenset()) -> None:
        if not name.strip() or len(name) > 150:
            raise ValueError("Role names must contain 1 to 150 characters.")
        for permission in permissions:
            PermissionName.parse(permission)
        self.roles[name] = self.roles.get(name, frozenset()) | frozenset(permissions)

    def validate(self) -> None:
        for name, permissions in self.roles.items():
            unknown = permissions - self.permissions.keys()
            if unknown:
                raise ValueError(f"Unknown permissions on {name!r}: {sorted(unknown)}.")


authorization_catalog = AuthorizationCatalog()
