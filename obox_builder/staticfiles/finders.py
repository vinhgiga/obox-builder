"""Static file discovery across installed apps and STATICFILES_DIRS."""

from __future__ import annotations

from pathlib import Path

from obox_builder.conf import settings

STATIC_DIR_NAME = "static"


def extra_static_dirs() -> list[Path]:
    """Directories configured via ``STATICFILES_DIRS`` (searched first)."""
    return [Path(directory) for directory in settings.STATICFILES_DIRS]


def app_static_dirs() -> list[Path]:
    """``static/`` directories of installed apps, in INSTALLED_APPS order."""
    from obox_builder.apps import apps

    directories: list[Path] = []
    for config in apps.get_app_configs():
        candidate = config.path / STATIC_DIR_NAME
        if candidate.is_dir():
            directories.append(candidate)
    return directories


def find_static_directories() -> list[Path]:
    """All static source directories; earlier entries win on filename conflicts."""
    return extra_static_dirs() + app_static_dirs()


def served_static_directories() -> list[Path]:
    """Directories actually served: collected STATIC_ROOT in production, sources otherwise."""
    static_root = settings.STATIC_ROOT
    if not settings.DEBUG and static_root is not None and Path(static_root).is_dir():
        return [Path(static_root)]
    return find_static_directories()
