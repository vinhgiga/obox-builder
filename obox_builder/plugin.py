"""Contracts for synchronous wiring and optional asynchronous resources."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager

    from obox_builder.application import Obox


@runtime_checkable
class Plugin(Protocol):
    name: str
    requires: tuple[str, ...]

    def setup(self, obox: Obox) -> None: ...


@runtime_checkable
class LifespanPlugin(Protocol):
    def lifespan(self, obox: Obox) -> AbstractAsyncContextManager[None]: ...
