"""File JSON contracts share the API's validation and serialization boundary."""

from typing import Literal
from uuid import UUID

from pydantic import Field

from obox_builder.builder.api_schema import ApiRequest, ApiResponse


class NewFile(ApiRequest):
    filename: str = Field(min_length=1, max_length=255)
    size: int = Field(gt=0, le=2**63 - 1, strict=True)
    field: str = Field(min_length=1, max_length=511)
    content_type: str = Field(default="application/octet-stream", max_length=127)
    owner_id: str | None = None


class RenameFile(ApiRequest):
    filename: str = Field(min_length=1, max_length=255)


class ReorderFiles(ApiRequest):
    field: str = Field(min_length=1, max_length=511)
    ids: list[UUID]


class FileMetadata(ApiResponse):
    id: UUID
    filename: str
    size: int
    content_type: str
    offset: int
    status: Literal["pending", "ready", "failed"]


class UploadCreated(FileMetadata):
    chunk_size: int


class ReorderedFiles(ApiResponse):
    ids: list[UUID]
