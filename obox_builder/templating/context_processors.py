"""Framework-level template context processors."""

from __future__ import annotations

from typing import Any

from fastapi import Request

from obox_builder.contrib.i18n import get_language, get_language_choices


def request(request: Request) -> dict[str, Any]:
    """Expose the current request and locale choices to every template."""
    return {
        "request": request,
        "language_code": getattr(request.state, "language_code", get_language()),
        "available_languages": get_language_choices(),
    }
