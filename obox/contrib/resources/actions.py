"""Metadata for explicitly registered builder actions."""

from collections.abc import Callable
from typing import Any, TypeVar, cast

F = TypeVar("F", bound=Callable[..., Any])


def action(*, description: str, permission: str = "update") -> Callable[[F], F]:
    def decorate(method: F) -> F:
        cast(Any, method).obox_action = description, permission
        return method

    return decorate
