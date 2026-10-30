"""Bounded private uploads for admin file fields."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import PurePosixPath
from typing import Any, BinaryIO
from uuid import uuid4

from starlette.datastructures import UploadFile

from obox_builder.conf import settings
from obox_builder.core.storage import default_storage
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
) -> tuple[dict[str, Any], list[tuple[str, bytes]], dict[str, list[str]]]:
    """Replace file inputs by random relative paths and return bytes to persist."""
    prepared = dict(data)
    staged: list[tuple[str, bytes]] = []
    errors: dict[str, list[str]] = {}
    total_bytes = 0
    for spec in specs:
        if not isinstance(spec.type, File):
            continue
        upload = prepared.get(spec.name)
        if not isinstance(upload, UploadFile) or not upload.filename:
            prepared[spec.name] = getattr(target, spec.name, None) if target is not None else None
            continue
        max_bytes = min(spec.type.max_bytes, settings.UPLOAD_MAX_BYTES)
        content = await upload.read(max_bytes + 1)
        await upload.close()
        total_bytes += len(content)
        if len(content) > max_bytes:
            prepared[spec.name] = getattr(target, spec.name, None) if target is not None else None
            errors[spec.name] = [f"File must be {max_bytes} bytes or smaller."]
            continue
        if total_bytes > settings.UPLOAD_MAX_BYTES:
            prepared[spec.name] = getattr(target, spec.name, None) if target is not None else None
            errors[spec.name] = [
                f"Total uploads must be {settings.UPLOAD_MAX_BYTES} bytes or smaller."
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
        relative = PurePosixPath("admin") / "uploads" / f"{uuid4().hex}{extension}"
        name = relative.as_posix()
        prepared[spec.name] = name
        staged.append((name, content))
    return prepared, staged, errors


def persist_uploads(staged: Sequence[tuple[str, bytes]]) -> None:
    created: list[str] = []
    try:
        for name, content in staged:
            created.append(default_storage.save(name, content))
    except BaseException:
        for name in created:
            default_storage.delete(name)
        raise


def discard_uploads(staged: Sequence[tuple[str, bytes]]) -> None:
    for name, _content in staged:
        default_storage.delete(name)


def open_upload(relative: str) -> BinaryIO | None:
    if not re.fullmatch(r"admin/uploads/[0-9a-f]{32}\.(?:jpg|png|webp|pdf|txt)", relative):
        return None
    try:
        return default_storage.open(relative)
    except FileNotFoundError:
        return None
