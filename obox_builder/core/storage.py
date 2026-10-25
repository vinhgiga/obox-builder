"""Private file storage with a configurable backend."""

from __future__ import annotations

import importlib
import os
import threading
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Protocol

from obox_builder.conf import settings
from obox_builder.core.exceptions import ImproperlyConfigured


class Storage(Protocol):
    def save(self, name: str, content: bytes) -> str: ...

    def open(self, name: str, mode: str = "rb") -> BinaryIO: ...

    def exists(self, name: str) -> bool: ...

    def delete(self, name: str) -> None: ...


class FileSystemStorage:
    """Local storage that writes private files with restrictive permissions."""

    def __init__(self, location: str | Path | None = None) -> None:
        self.location = Path(location) if location is not None else None

    @property
    def root(self) -> Path:
        root = (self.location or Path(settings.MEDIA_ROOT)).resolve()
        for public_root in [settings.STATIC_ROOT, *settings.STATICFILES_DIRS]:
            if public_root is not None and root.is_relative_to(Path(public_root).resolve()):
                raise ImproperlyConfigured(
                    "MEDIA_ROOT must not be inside a public static directory."
                )
        return root

    def save(self, name: str, content: bytes) -> str:
        path = self._path(name)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor: int | None = None
        created = False
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            created = True
            with os.fdopen(descriptor, "wb") as file:
                descriptor = None
                file.write(content)
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
            if created:
                path.unlink(missing_ok=True)
            raise
        return PurePosixPath(name).as_posix()

    def open(self, name: str, mode: str = "rb") -> BinaryIO:
        if mode != "rb":
            raise ValueError("FileSystemStorage.open() supports read-only binary mode.")
        return self._path(name).open("rb")

    def exists(self, name: str) -> bool:
        return self._path(name).is_file()

    def delete(self, name: str) -> None:
        self._path(name).unlink(missing_ok=True)

    def _path(self, name: str) -> Path:
        key = PurePosixPath(name)
        if (
            not name
            or "\\" in name
            or key.is_absolute()
            or any(part in {"", ".", ".."} for part in key.parts)
        ):
            raise ValueError("Storage names must be safe relative paths.")
        root = self.root
        path = root.joinpath(*key.parts)
        if not key.parts or not path.resolve().is_relative_to(root):
            raise ValueError("Storage names must stay within the configured storage root.")
        return path


class DefaultStorage:
    """Lazy proxy that reloads the backend when its dotted path changes."""

    def __init__(self) -> None:
        self._backend_path = ""
        self._backend: Storage | None = None
        self._lock = threading.Lock()

    def _get_backend(self) -> Storage:
        path = settings.STORAGE_BACKEND
        with self._lock:
            if path != self._backend_path or self._backend is None:
                module_path, separator, name = path.rpartition(".")
                if not separator:
                    raise ImproperlyConfigured("STORAGE_BACKEND must be a dotted path.")
                try:
                    backend_class = getattr(importlib.import_module(module_path), name)
                    backend = backend_class()
                except (ImportError, AttributeError, TypeError) as exc:
                    raise ImproperlyConfigured(
                        f"Cannot load storage backend {path!r}: {exc}"
                    ) from exc
                if not all(
                    callable(getattr(backend, method, None))
                    for method in ("save", "open", "exists", "delete")
                ):
                    raise ImproperlyConfigured(
                        f"Storage backend {path!r} must define save(), open(), exists() "
                        "and delete()."
                    )
                self._backend_path = path
                self._backend = backend
            if self._backend is None:
                raise ImproperlyConfigured(f"Storage backend {path!r} could not be initialized.")
            return self._backend

    def save(self, name: str, content: bytes) -> str:
        return self._get_backend().save(name, content)

    def open(self, name: str, mode: str = "rb") -> BinaryIO:
        return self._get_backend().open(name, mode)

    def exists(self, name: str) -> bool:
        return self._get_backend().exists(name)

    def delete(self, name: str) -> None:
        self._get_backend().delete(name)


default_storage = DefaultStorage()
