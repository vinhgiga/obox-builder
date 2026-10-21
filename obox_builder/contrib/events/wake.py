"""Process-local hints only; every worker always polls the durable queue."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager

_listeners: set[tuple[asyncio.AbstractEventLoop, asyncio.Event, frozenset[str]]] = set()


@contextmanager
def listen(queues: tuple[str, ...]) -> Iterator[asyncio.Event]:
    signal = asyncio.Event()
    entry = (asyncio.get_running_loop(), signal, frozenset(queues))
    _listeners.add(entry)
    try:
        yield signal
    finally:
        _listeners.discard(entry)


def notify(queues: set[str]) -> None:
    for loop, signal, interested in tuple(_listeners):
        if interested.intersection(queues):
            try:
                loop.call_soon_threadsafe(signal.set)
            except RuntimeError:
                _listeners.discard((loop, signal, interested))
