"""JSON aliases live at the HTTP boundary; services keep Python field names."""

from typing import Any

from pydantic import BaseModel, ConfigDict
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
    field: str
    pointer: str
    code: str
    detail: str
    meta: dict[str, Any] | None = None


class Problem(ApiResponse):
    type: str
    title: str
    status: int
    code: str
    detail: str
    instance: str
    trace_id: str
    errors: list[FieldError]
