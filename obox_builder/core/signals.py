from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import TypeVar

Receiver = Callable[..., object]

T = TypeVar("T", bound=Receiver)


class Signal:
    """Minimal signal: receivers are called with keyword arguments."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._receivers: dict[Receiver, str | None] = {}

    def connect(
        self, receiver: Receiver, sender: str | None = None, dispatch_uid: str | None = None
    ) -> None:
        if dispatch_uid is not None and any(
            uid == dispatch_uid for uid in self._receivers.values()
        ):
            return
        if receiver not in self._receivers:
            self._receivers[receiver] = dispatch_uid

    def disconnect(self, receiver: Receiver) -> bool:
        return self._receivers.pop(receiver, _MISSING) is not _MISSING

    def clear(self) -> None:
        self._receivers.clear()

    def send(self, sender: object | None = None, **kwargs: object) -> list[object]:
        results = []
        for receiver in list(self._receivers):
            results.append(receiver(sender=sender, **kwargs))
        return results

    async def send_async(self, sender: object | None = None, **kwargs: object) -> list[object]:
        results = []
        for receiver in list(self._receivers):
            result = receiver(sender=sender, **kwargs)
            if inspect.isawaitable(result):
                result = await result
            results.append(result)
        return results


_MISSING = object()

post_migrate = Signal("post_migrate")
