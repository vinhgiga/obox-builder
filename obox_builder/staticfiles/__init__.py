"""Static files: discovery, serving in development and collection."""

from obox_builder.conf import settings
from obox_builder.staticfiles.app import AppStaticFiles
from obox_builder.staticfiles.collect import CollectResult, collect_static
from obox_builder.staticfiles.finders import (
    app_static_dirs,
    extra_static_dirs,
    find_static_directories,
)


def static(path: str) -> str:
    """Absolute static URL for ``path`` (e.g. ``static("admin/css/admin.css")``)."""
    base = settings.STATIC_URL or "/static/"
    if not base.endswith("/"):
        base += "/"
    return base + path.lstrip("/")


__all__ = [
    "AppStaticFiles",
    "CollectResult",
    "app_static_dirs",
    "collect_static",
    "extra_static_dirs",
    "find_static_directories",
    "static",
]
