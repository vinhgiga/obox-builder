from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import NamedTuple, TypeVar, overload

from obox_builder.core.checks.messages import CheckMessage

CheckFunction = Callable[[], Sequence[CheckMessage]]

F = TypeVar("F", bound=CheckFunction)


class RegisteredCheck(NamedTuple):
    func: CheckFunction
    tags: tuple[str, ...]
    deploy: bool


_checks: list[RegisteredCheck] = []


@overload
def register(check: F, *, tags: Iterable[str] = (), deploy: bool = False) -> F: ...


@overload
def register(*, tags: Iterable[str] = (), deploy: bool = False) -> Callable[[F], F]: ...


def register(
    check: F | None = None, *, tags: Iterable[str] = (), deploy: bool = False
) -> F | Callable[[F], F]:
    """Register a check function, optionally under tags and as a deployment check."""

    def _register(func: F) -> F:
        _checks.append(RegisteredCheck(func, tuple(tags), deploy))
        return func

    if check is not None:
        return _register(check)
    return _register


def get_checks(tags: Sequence[str] | None = None, *, deploy: bool = False) -> list[CheckFunction]:
    selected = [entry for entry in _checks if deploy or not entry.deploy]
    if tags is not None:
        wanted = set(tags)
        selected = [entry for entry in selected if not wanted.isdisjoint(entry.tags)]
    result: list[CheckFunction] = []
    seen: set[int] = set()
    for entry in selected:
        key = id(entry.func)
        if key in seen:
            continue
        seen.add(key)
        result.append(entry.func)
    return result


def run_checks(tags: Sequence[str] | None = None, *, deploy: bool = False) -> list[CheckMessage]:
    messages: list[CheckMessage] = []
    for check in get_checks(tags, deploy=deploy):
        messages.extend(check())
    return messages
