"""A Starlette ``StaticFiles`` implementation over registry-derived directories."""

from __future__ import annotations

from collections.abc import Sequence
from os import PathLike, stat_result
from pathlib import Path
from typing import Any

from starlette.datastructures import QueryParams
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from obox_builder.exceptions import ImproperlyConfigured


class AppStaticFiles(StaticFiles):
    """Serve static files from a list of directories (first hit wins)."""

    def __init__(self, directories: Sequence[Path], **kwargs: Any) -> None:
        paths: list[str | PathLike[str]] = [str(Path(directory)) for directory in directories]
        if not paths:
            raise ImproperlyConfigured("AppStaticFiles needs at least one directory.")
        super().__init__(directory=paths[0], check_dir=False, **kwargs)
        self.all_directories = paths

    def file_response(
        self,
        full_path: str | PathLike[str],
        stat_result: stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        version = QueryParams(scope["query_string"]).get("v")
        # Only the current file version is safe to reuse without revalidation.
        if version == str(stat_result.st_mtime_ns):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "no-cache"
        return response
