"""Shared checks for reusable configuration constraints."""

from collections.abc import Iterable
from typing import Any

from obox.core.exceptions import ImproperlyConfigured


def require_positive_integers(values: Iterable[Any], message: str) -> None:
    if any(type(value) is not int or value < 1 for value in values):
        raise ImproperlyConfigured(message)
