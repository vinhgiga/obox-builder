"""Static files: discovery, serving in development and collection."""

from __future__ import annotations

from pathlib import Path

from obox_builder.conf import settings
from obox_builder.staticfiles.app import AppStaticFiles
from obox_builder.staticfiles.collect import CollectResult, collect_static
from obox_builder.staticfiles.finders import (
    app_static_dirs,
    extra_static_dirs,
    find_static_directories,
    served_static_directories,
)


def static(path: str) -> str:
    """Absolute static URL for ``path`` (e.g. ``static("admin/css/admin.css")``).

    Appends a ``?v=`` query versioned by file mtime so browsers pick up edits
    without a hard refresh; unresolvable paths keep the plain URL.
    """
    base = settings.STATIC_URL or "/static/"
    if not base.endswith("/"):
        base += "/"
    url = base + path.lstrip("/")
    version = _static_version(path)
    if version is None:
        return url
    return f"{url}?v={version}"


def _static_version(path: str) -> int | None:
    relative = Path(path.lstrip("/"))
    for directory in served_static_directories():
        candidate = directory / relative
        if candidate.is_file():
            try:
                return candidate.stat().st_mtime_ns
            except OSError:
                return None
    return None


__all__ = [
    "AppStaticFiles",
    "CollectResult",
    "app_static_dirs",
    "collect_static",
    "extra_static_dirs",
    "find_static_directories",
    "served_static_directories",
    "static",
]
