"""Fail-closed authorization; identity resolution belongs to the host application."""

from __future__ import annotations

from dataclasses import dataclass
from inspect import iscoroutinefunction
from typing import Any, Protocol, TypeVar

T = TypeVar("T")


def _valid_permission(name: str) -> bool:
    parts = name.split(".")
    return len(parts) in (2, 3) and all(part.isidentifier() for part in parts)


def has_permission(actor: Any, permission: str) -> bool:
    check = getattr(actor, "has_perm", None)
    return bool(
        getattr(actor, "is_authenticated", False) is True
        and getattr(actor, "is_active", False) is True
        and _valid_permission(permission)
        and callable(check)
        and not iscoroutinefunction(check)
        and check(permission) is True
    )


@dataclass(frozen=True)
class Permission:
    name: str
    label: str
    kind: str


class Identity(Protocol):
    """Minimal identity returned by authentication; has_perm is synchronous."""

    @property
    def id(self) -> Any: ...

    @property
    def username(self) -> str: ...

    @property
    def is_authenticated(self) -> bool: ...

    @property
    def is_active(self) -> bool: ...

    def has_perm(self, permission: str) -> bool: ...


@dataclass(frozen=True)
class Principal:
    id: Any = None
    is_authenticated: bool = False
    is_active: bool = True
    username: str = ""
    is_superuser: bool = False
    roles: frozenset[str] = frozenset()
    permissions: frozenset[str] = frozenset()

    def has_perm(self, permission: str) -> bool:
        return (
            self.is_authenticated
            and self.is_active
            and _valid_permission(permission)
            and (self.is_superuser or permission in self.permissions)
        )


@dataclass(frozen=True)
class ScopedIdentity:
    owner: Identity
    api_scopes: frozenset[str]

    def __getattr__(self, name: str) -> Any:
        return getattr(self.owner, name)

    @property
    def id(self) -> Any:
        return self.owner.id

    @property
    def username(self) -> str:
        return self.owner.username

    @property
    def is_authenticated(self) -> bool:
        return self.owner.is_authenticated

    @property
    def is_active(self) -> bool:
        return self.owner.is_active

    def has_perm(self, permission: str) -> bool:
        return permission in self.api_scopes and self.owner.has_perm(permission)


ANONYMOUS = Principal()
SYSTEM = Principal(id="system", is_authenticated=True, roles=frozenset({"system"}))
