"""A Starlette ``StaticFiles`` implementation over registry-derived directories."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from starlette.staticfiles import StaticFiles

from obox_builder.core.exceptions import ImproperlyConfigured


class AppStaticFiles(StaticFiles):
    """Serve static files from a list of directories (first hit wins)."""

    def __init__(self, directories: Sequence[Path], **kwargs: Any) -> None:
        paths = [str(Path(directory)) for directory in directories]
        if not paths:
            raise ImproperlyConfigured("AppStaticFiles needs at least one directory.")
        super().__init__(directory=paths[0], check_dir=False, **kwargs)
        self.all_directories = paths
