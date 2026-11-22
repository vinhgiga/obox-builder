from collections.abc import Callable
from typing import Any, TypeVar, overload

F = TypeVar("F", bound=Callable[..., Any])


@overload
def action(function: F, *, permissions: Any = None, description: str | None = None) -> F: ...


@overload
def action(
    function: None = None, *, permissions: Any = None, description: str | None = None
) -> Callable[[F], F]: ...


def action(function: Any = None, *, permissions: Any = None, description: str | None = None) -> Any:
    def decorate(method: Any) -> Any:
        if permissions is not None:
            method.allowed_permissions = permissions
        if description is not None:
            method.short_description = description
        return method

    return decorate(function) if function is not None else decorate
