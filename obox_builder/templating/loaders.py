"""Template loading from installed apps and configured directories."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from jinja2 import BaseLoader, TemplateNotFound
from jinja2.loaders import split_template_path

if TYPE_CHECKING:
    from jinja2 import Environment

TEMPLATES_DIR_NAME = "templates"


def collect_template_dirs() -> list[Path]:
    """Search paths: TEMPLATE_DIRS first, then each app's ``templates/``."""
    from obox_builder.apps import apps
    from obox_builder.conf import settings

    directories: list[Path] = [Path(directory) for directory in settings.TEMPLATE_DIRS]
    for config in apps.get_app_configs():
        candidate = config.path / TEMPLATES_DIR_NAME
        if candidate.is_dir():
            directories.append(candidate)
    return directories


class AppDirectoriesLoader(BaseLoader):
    """A filesystem loader over a fixed, registry-derived search path list."""

    def __init__(self, search_paths: list[Path]) -> None:
        self.search_paths = search_paths

    def get_source(
        self, environment: Environment, template: str
    ) -> tuple[str, str, Callable[[], bool]]:
        pieces = split_template_path(template)
        for search_path in self.search_paths:
            filename = os.path.join(search_path, *pieces)
            if not os.path.isfile(filename):
                continue
            with open(filename, encoding="utf-8") as file:
                source = file.read()
            mtime = os.path.getmtime(filename)

            def uptodate(filename: str = filename, mtime: float = mtime) -> bool:
                try:
                    return os.path.getmtime(filename) == mtime
                except OSError:
                    return False

            return source, filename, uptodate
        raise TemplateNotFound(template)

    def list_templates(self) -> list[str]:
        found: set[str] = set()
        for search_path in self.search_paths:
            if not search_path.is_dir():
                continue
            for root, _, filenames in os.walk(search_path):
                for filename in filenames:
                    full = Path(root) / filename
                    found.add(full.relative_to(search_path).as_posix())
        return sorted(found)
