"""Bounded private uploads for admin file fields."""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

from starlette.datastructures import UploadFile

from obox_builder.conf import settings
from obox_builder.core.exceptions import ImproperlyConfigured
from obox_builder.fields import FieldSpec
from obox_builder.fields.types.file import File


def _is_utf8_text(data: bytes) -> bool:
    try:
        return b"\0" not in data and data.decode("utf-8") is not None
    except UnicodeDecodeError:
        return False


_FORMATS = {
    "image/jpeg": (".jpg", lambda data: data.startswith(b"\xff\xd8\xff")),
    "image/png": (".png", lambda data: data.startswith(b"\x89PNG\r\n\x1a\n")),
    "image/webp": (".webp", lambda data: data.startswith(b"RIFF") and data[8:12] == b"WEBP"),
    "application/pdf": (".pdf", lambda data: data.startswith(b"%PDF-")),
    "text/plain": (".txt", _is_utf8_text),
}


async def stage_uploads(
    data: dict[str, Any], specs: Sequence[FieldSpec], target: Any = None
) -> tuple[dict[str, Any], list[tuple[Path, bytes]], dict[str, list[str]]]:
    """Replace file inputs by random relative paths and return bytes to persist."""
    prepared = dict(data)
    staged: list[tuple[Path, bytes]] = []
    errors: dict[str, list[str]] = {}
    total_bytes = 0
    for spec in specs:
        if not isinstance(spec.type, File):
            continue
        upload = prepared.get(spec.name)
        if not isinstance(upload, UploadFile) or not upload.filename:
            prepared[spec.name] = getattr(target, spec.name, None) if target is not None else None
            continue
        max_bytes = min(spec.type.max_bytes, settings.ADMIN_MAX_UPLOAD_BYTES)
        content = await upload.read(max_bytes + 1)
        await upload.close()
        total_bytes += len(content)
        if len(content) > max_bytes:
            prepared[spec.name] = getattr(target, spec.name, None) if target is not None else None
            errors[spec.name] = [f"File must be {max_bytes} bytes or smaller."]
            continue
        if total_bytes > settings.ADMIN_MAX_UPLOAD_BYTES:
            prepared[spec.name] = getattr(target, spec.name, None) if target is not None else None
            errors[spec.name] = [
                f"Total uploads must be {settings.ADMIN_MAX_UPLOAD_BYTES} bytes or smaller."
            ]
            continue
        detected = next(
            (mime for mime, (_extension, check) in _FORMATS.items() if check(content)), None
        )
        if detected is None or detected not in spec.type.accept:
            prepared[spec.name] = getattr(target, spec.name, None) if target is not None else None
            errors[spec.name] = ["Choose a supported image, PDF, or text file."]
            continue
        extension = _FORMATS[detected][0]
        relative = Path("admin") / "uploads" / f"{uuid4().hex}{extension}"
        prepared[spec.name] = relative.as_posix()
        staged.append((Path(settings.MEDIA_ROOT) / relative, content))
    return prepared, staged, errors


def persist_uploads(staged: Sequence[tuple[Path, bytes]]) -> None:
    if staged:
        _ensure_private_media_root()
    created = []
    try:
        for path, content in staged:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            created.append(path)
            with os.fdopen(descriptor, "wb") as upload:
                upload.write(content)
    except OSError:
        for path in created:
            path.unlink(missing_ok=True)
        raise


def discard_uploads(staged: Sequence[tuple[Path, bytes]]) -> None:
    for path, _content in staged:
        path.unlink(missing_ok=True)


def resolve_upload(relative: str) -> Path | None:
    if not re.fullmatch(r"admin/uploads/[0-9a-f]{32}\.(?:jpg|png|webp|pdf|txt)", relative):
        return None
    root = Path(settings.MEDIA_ROOT).resolve()
    _ensure_private_media_root(root)
    path = (root / relative).resolve()
    return path if path.is_relative_to(root) and path.is_file() else None


def _ensure_private_media_root(root: Path | None = None) -> None:
    media_root = root or Path(settings.MEDIA_ROOT).resolve()
    public_roots = [settings.STATIC_ROOT, *settings.STATICFILES_DIRS]
    for public_root in public_roots:
        if public_root is not None and media_root.is_relative_to(Path(public_root).resolve()):
            raise ImproperlyConfigured("MEDIA_ROOT must not be inside a public static directory.")
