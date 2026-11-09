from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, StrictStr, field_serializer

from obox_builder.builder.api import normalize_datetime
from obox_builder.builder.api_schema import ApiRequest, ApiResponse


class SubscriptionInput(ApiRequest):
    resource: StrictStr = Field(min_length=1, max_length=255)
    url: StrictStr = Field(min_length=1, max_length=4096)
    actions: list[Literal["create", "update"]] = Field(default=["create", "update"], min_length=1)


class SubscriptionRead(ApiResponse):
    id: UUID
    resource: str
    url: str
    actions: list[Literal["create", "update"]]
    enabled: bool
    created_at: datetime
    expires_at: datetime

    dates = field_serializer("created_at", "expires_at")(normalize_datetime)


class SubscriptionCreated(SubscriptionRead):
    secret: str


class Subscriptions(ApiResponse):
    items: list[SubscriptionRead]


class Enabled(ApiRequest):
    enabled: bool = Field(strict=True)


class ChangeNotification(ApiResponse):
    id: UUID
    version: Literal["1"] = "1"
    type: Literal["record.created", "record.updated"]
    resource: str
    action: Literal["create", "update"]
    record_id: str
    occurred_at: datetime

    date = field_serializer("occurred_at")(normalize_datetime)


class DeliveryRead(ApiResponse):
    id: UUID
    sequence: int
    event: ChangeNotification
    status: Literal["pending", "sending", "retry", "delivered", "failed", "skipped"]
    attempts: int
    last_status: int | None
    last_error: str | None


class Deliveries(ApiResponse):
    items: list[DeliveryRead]
    next_cursor: int
    has_more: bool
