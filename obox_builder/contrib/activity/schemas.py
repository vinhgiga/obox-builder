"""Typed JSON projections for the record activity endpoint."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, JsonValue, StrictStr, field_serializer, field_validator

from obox_builder.builder.api import (
    Page,
    json_value,
    normalize_datetime,
    normalize_optional_datetime,
)
from obox_builder.builder.api_schema import ApiRequest, ApiResponse
from obox_builder.builder.fields import _validate_text


class Actor(ApiResponse):
    id: str | None
    display_name: str


class Change(ApiResponse):
    field: str
    kind: str
    before: JsonValue = None
    after: JsonValue = None

    encode_values = field_serializer("before", "after")(json_value)


class CommentPermissions(ApiResponse):
    update: bool
    delete: bool


class CommentRead(ApiResponse):
    id: str
    author: Actor
    body: str
    created_at: datetime
    edited_at: datetime | None
    permissions: CommentPermissions

    encode_created_at = field_serializer("created_at")(normalize_datetime)
    encode_edited_at = field_serializer("edited_at")(normalize_optional_datetime)


class CommentInput(ApiRequest):
    body: StrictStr

    @field_validator("body")
    @classmethod
    def clean_body(cls, value: str) -> str:
        value = _validate_text(value).replace("\r\n", "\n").replace("\r", "\n")
        if not value.strip():
            raise ValueError("Enter a comment.")
        return value


class CommentPage(ApiResponse):
    items: list[CommentRead]
    page: Page


class ActivityItem(ApiResponse):
    id: str
    actor: Actor
    source: str
    occurred_at: datetime

    encode_occurred_at = field_serializer("occurred_at")(normalize_datetime)


class RevisionItem(ActivityItem):
    kind: Literal["revision"]
    action: Literal["create", "update", "delete"]
    changes: list[Change] = Field(min_length=1)


class CommentItem(ActivityItem):
    kind: Literal["comment"]
    comment: CommentRead


class ActivityPage(ApiResponse):
    items: list[Annotated[RevisionItem | CommentItem, Field(discriminator="kind")]]
    page: Page
