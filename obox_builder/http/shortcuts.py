"""HTTP shortcuts shared by views: rendering and redirects."""

from __future__ import annotations

from starlette.responses import RedirectResponse

from obox_builder.templating import render as render
from obox_builder.templating import render_template as render_template

__all__ = ["redirect", "render", "render_template"]


def redirect(url: str, *, status_code: int = 302) -> RedirectResponse:
    """Redirect to ``url`` (default 302 Found)."""
    return RedirectResponse(url, status_code=status_code)
