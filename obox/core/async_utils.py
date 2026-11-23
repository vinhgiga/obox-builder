"""Await extension hooks when their implementation performs I/O."""

from inspect import isawaitable
from typing import Any


async def invoke(method: Any, *args: Any, **kwargs: Any) -> Any:
    result = method(*args, **kwargs)
    return await result if isawaitable(result) else result
