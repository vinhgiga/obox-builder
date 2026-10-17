from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, overload


class CommandError(Exception):
    """Raised by a command to abort with a clean message and exit code 1."""


def _first_doc_line(func: Callable[..., Any]) -> str:
    doc = inspect.getdoc(func)
    return doc.splitlines()[0] if doc else ""


@dataclass(frozen=True)
class CommandSpec:
    """Metadata for a command module's single ``handle`` entry point."""

    func: Callable[..., Any]
    help: str = ""
    requires_settings: bool = True
    requires_checks: bool = False
    hidden: bool = False

    @property
    def is_async(self) -> bool:
        return inspect.iscoroutinefunction(self.func)


@overload
def command(
    func: Callable[..., Any],
    *,
    help: str | None = None,
    requires_settings: bool = True,
    requires_checks: bool = False,
    hidden: bool = False,
) -> CommandSpec: ...


@overload
def command(
    *,
    help: str | None = None,
    requires_settings: bool = True,
    requires_checks: bool = False,
    hidden: bool = False,
) -> Callable[[Callable[..., Any]], CommandSpec]: ...


def command(
    func: Callable[..., Any] | None = None,
    *,
    help: str | None = None,
    requires_settings: bool = True,
    requires_checks: bool = False,
    hidden: bool = False,
) -> CommandSpec | Callable[[Callable[..., Any]], CommandSpec]:
    """Turn the module's ``handle`` function into a ``CommandSpec``.

    Usable bare (``@command``) or with options (``@command(help="...", ...)``).
    """

    def _build(target: Callable[..., Any]) -> CommandSpec:
        return CommandSpec(
            func=target,
            help=help or _first_doc_line(target),
            requires_settings=requires_settings,
            requires_checks=requires_checks,
            hidden=hidden,
        )

    if func is not None:
        return _build(func)
    return _build
