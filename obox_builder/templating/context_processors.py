"""Framework-level template context processors."""

from __future__ import annotations

from typing import Any

from fastapi import Request


def request(request: Request) -> dict[str, Any]:
    """Expose the current ``request`` to every template."""
    return {"request": request}
