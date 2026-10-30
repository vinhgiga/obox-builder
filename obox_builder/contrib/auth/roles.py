"""Immutable initial role templates; administrator edits remain authoritative."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from importlib import import_module
from importlib.util import find_spec
from types import MappingProxyType

from obox_builder.exceptions import ImproperlyConfigured
from obox_builder.security import PermissionRef


@dataclass(frozen=True)
class RoleTemplate:
    name: str
    permissions: frozenset[str | PermissionRef] = frozenset()

    def __post_init__(self) -> None:
        if not self.name.strip() or len(self.name) > 150:
            raise ImproperlyConfigured("Role names must contain 1 to 150 characters.")
        names = frozenset(p.name if isinstance(p, PermissionRef) else p for p in self.permissions)
        for name in names:
            PermissionRef.parse(name)
        object.__setattr__(self, "permissions", names)


def discover_roles(packages: Iterable[str]) -> Mapping[str, frozenset[str]]:
    roles: dict[str, frozenset[str]] = {}
    for package in packages:
        path = f"{package}.roles"
        if find_spec(path) is None:
            continue
        for value in vars(import_module(path)).values():
            if isinstance(value, RoleTemplate):
                names = frozenset(
                    p.name if isinstance(p, PermissionRef) else p for p in value.permissions
                )
                roles[value.name] = roles.get(value.name, frozenset()) | names
    return MappingProxyType(roles)
