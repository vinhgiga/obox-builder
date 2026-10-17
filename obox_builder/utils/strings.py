"""Utilities for user-facing strings (no layer dependencies)."""

from __future__ import annotations

import re

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def camel_to_snake(name: str) -> str:
    """``OrderItem`` -> ``order_item``, ``HTTPResponseLog`` -> ``http_response_log``."""
    return _CAMEL_BOUNDARY.sub("_", name).lower()


def humanize(name: str) -> str:
    """``order_item`` -> ``Order item``."""
    return name.replace("_", " ").strip().capitalize()
