"""Versioned package assets and revalidated editable-install assets."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any

import anyio.to_thread
from starlette.datastructures import Headers, URLPath
from starlette.exceptions import HTTPException
from starlette.responses import FileResponse, Response
from starlette.routing import Mount
from starlette.staticfiles import NotModifiedResponse, StaticFiles
from starlette.types import Receive, Scope, Send

from obox.web.assets import SOURCES, collect_assets, load_manifest

PACKAGE = Path(__file__).parents[1]


class AssetResponse(FileResponse):
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if_range = Headers(scope=scope).get("if-range")
        if if_range is not None and "last-modified" not in self.headers:
            matches = if_range == self.headers.get("etag")
            scope = {
                **scope,
                "headers": [
                    (name, value)
                    for name, value in scope["headers"]
                    if name != b"if-range" and (name != b"range" or matches)
                ],
            }
        await super().__call__(scope, receive, send)


class AssetFiles(StaticFiles):
    def __init__(self, namespace: str, root: str | os.PathLike[str] | None = None) -> None:
        manifest = load_manifest(PACKAGE / "_static")
        self.fingerprint: str | None = None
        self.packaged = manifest is not None and root is None
        if root is not None:
            self.fingerprint = collect_assets(PACKAGE, Path(root))[namespace]
            super().__init__(directory=Path(root) / namespace)
        else:
            if manifest is not None:
                self.fingerprint = manifest[namespace]
            directories = [PACKAGE / source for source in SOURCES[namespace]]
            super().__init__(directory=directories[0])
            self.all_directories = list(directories)

    async def get_response(self, path: str, scope: Scope) -> Response:
        parts = Path(path).parts
        first = parts[0] if parts else ""
        immutable = self.fingerprint is not None and bool(re.fullmatch(r"[0-9a-f]{64}", first))
        try:
            if self.packaged and immutable:
                if first != self.fingerprint:
                    raise HTTPException(404)
                path = str(Path(*parts[1:]))
            elif self.fingerprint is not None and not immutable and not self.packaged:
                path = os.path.join(self.fingerprint, path)
            response = await super().get_response(path, scope)
        except HTTPException as error:
            error.headers = {**(error.headers or {}), "Cache-Control": "no-store"}
            raise
        response.headers["Cache-Control"] = (
            "public, max-age=31536000, immutable" if immutable else "no-cache"
        )
        if isinstance(response, FileResponse):
            if not immutable:
                content = await anyio.to_thread.run_sync(Path(response.path).read_bytes)
                response.headers["ETag"] = f'"{hashlib.sha256(content).hexdigest()}"'
                del response.headers["Last-Modified"]
            if self.is_not_modified(response.headers, Headers(scope=scope)):
                return NotModifiedResponse(response.headers)
        return response

    def file_response(
        self,
        full_path: str | os.PathLike[str],
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        # Evaluate validators after applying the cache policy and content ETag.
        return AssetResponse(full_path, stat_result=stat_result, status_code=status_code)


class AssetMount(Mount):
    def __init__(
        self,
        path: str,
        *,
        namespace: str,
        name: str,
        root: str | os.PathLike[str] | None = None,
    ) -> None:
        assets = AssetFiles(namespace, root)
        self.fingerprint = assets.fingerprint
        super().__init__(path, app=assets, name=name)

    def url_path_for(self, name: str, /, **path_params: Any) -> URLPath:
        if name == self.name and "path" in path_params and self.fingerprint is not None:
            path_params["path"] = self.fingerprint + "/" + str(path_params["path"]).lstrip("/")
        return super().url_path_for(name, **path_params)
