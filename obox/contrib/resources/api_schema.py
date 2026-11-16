"""JSON aliases live at the HTTP boundary; services keep Python field names."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field
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
