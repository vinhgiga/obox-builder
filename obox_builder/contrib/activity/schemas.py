"""Typed JSON projections for the record activity endpoint."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, JsonValue, field_serializer

from obox_builder.builder.api import Page, json_value
from obox_builder.builder.api_schema import ApiResponse


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

    encode_times = field_serializer("created_at", "edited_at")(json_value)


class ActivityItem(ApiResponse):
    id: str
    actor: Actor
    source: str
    occurred_at: datetime

    encode_time = field_serializer("occurred_at")(json_value)


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
