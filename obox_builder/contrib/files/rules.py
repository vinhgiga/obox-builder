"""Explicit media allowlists and per-field upload limits."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

from obox_builder.errors import BuilderValidationError, ImproperlyConfigured

IMAGES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".avif": "image/avif",
}
VIDEOS = {".mp4": "video/mp4", ".webm": "video/webm"}


@dataclass(frozen=True)
class Rule:
    types: Mapping[str, str] | None = None
    max_size: int | None = None
    min_count: int = 0
    max_count: int | None = None

    def __post_init__(self) -> None:
        if (
            (
                self.types is not None
                and (
                    not self.types
                    or any(
                        not extension.startswith(".")
                        or extension != extension.lower()
                        or not media_type
                        or len(media_type) > 127
                        for extension, media_type in self.types.items()
                    )
                )
            )
            or (self.max_size is not None and self.max_size < 1)
            or self.min_count < 0
            or (self.max_count is not None and self.max_count < self.min_count)
        ):
            raise ImproperlyConfigured(
                "Use valid file types, a positive size and consistent counts."
            )

    def check(self, filename: str, size: int) -> str:
        media_type = (
            self.types.get(PurePosixPath(filename).suffix.lower())
            if self.types is not None
            else "application/octet-stream"
        )
        if (
            not filename
            or len(filename) > 255
            or filename in {".", ".."}
            or not media_type
            or size < 1
            or (self.max_size is not None and size > self.max_size)
            or any(ord(char) < 32 or ord(char) == 127 or char in "/\\" for char in filename)
        ):
            raise BuilderValidationError({"filename": ["File type, name or size is not allowed."]})
        return media_type

    def count(self, field: str, size: int) -> None:
        if size < self.min_count or (self.max_count is not None and size > self.max_count):
            raise BuilderValidationError(
                {field: ["Attachment count is outside the allowed range."]}
            )


def sniff(head: bytes) -> str | None:
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head[:6] in {b"GIF87a", b"GIF89a"}:
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[4:8] == b"ftyp" and head[8:12] in {b"avif", b"avis"}:
        return "image/avif"
    if head[4:8] == b"ftyp" and head[8:12] in {
        b"isom",
        b"iso2",
        b"mp41",
        b"mp42",
        b"avc1",
        b"M4V ",
        b"MSNV",
        b"dash",
    }:
        return "video/mp4"
    if head.startswith(b"\x1a\x45\xdf\xa3") and b"webm" in head:
        return "video/webm"
    return None
