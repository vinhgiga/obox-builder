"""JSON aliases live at the HTTP boundary; services keep Python field names."""

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from pydantic.alias_generators import to_camel


class ApiRequest(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        validate_by_alias=True,
        validate_by_name=False,
        extra="forbid",
    )


class ApiResponse(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        validate_by_name=True,
        serialize_by_alias=True,
        from_attributes=True,
        extra="forbid",
    )


class FieldError(ApiResponse):
    field: str = Field(min_length=1)
    pointer: str = Field(
        pattern=r"^#(?:/(?:[^~/]|~[01])*)*$",
        description="RFC 6901 JSON Pointer into the failed request.",
    )
    code: str
    detail: str
    meta: dict[str, Any] | None = None


class Problem(ApiResponse):
    type: str = Field(
        min_length=1,
        json_schema_extra={"format": "uri-reference"},
    )
    title: str
    status: int = Field(ge=100, le=599)
    code: str
    detail: str
    instance: str = Field(
        min_length=1,
        json_schema_extra={"format": "uri-reference"},
    )
    trace_id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,128}$")
    errors: list[FieldError]


class Page(ApiResponse):
    limit: int | None = Field(ge=0)
    offset: int = Field(ge=0)
    has_more: bool
    next_cursor: str | None
    total: int | None = Field(ge=0)


def json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return (
            value.replace(tzinfo=value.tzinfo or UTC)
            .astimezone(UTC)
            .isoformat()
            .replace("+00:00", "Z")
        )
    if isinstance(value, int) and abs(value) > 2**53 - 1:
        return str(value)
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [json_value(item) for item in value]
    return TypeAdapter[Any](Any).dump_python(value, mode="json")


def normalize_datetime(value: datetime) -> datetime:
    return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC)


def normalize_optional_datetime(value: datetime | None) -> datetime | None:
    return None if value is None else normalize_datetime(value)
