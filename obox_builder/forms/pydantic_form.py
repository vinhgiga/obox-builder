"""Small reusable form wrapper for user-defined Pydantic models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError


@dataclass
class PydanticForm:
    """Validate submitted data and expose cleaned values and field errors."""

    schema: type[BaseModel]
    data: Mapping[str, Any]
    parsed: BaseModel | None = field(init=False, default=None)
    cleaned_data: dict[str, Any] = field(init=False, default_factory=dict)
    errors: dict[str, list[str]] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        try:
            self.parsed = self.schema.model_validate(dict(self.data))
        except ValidationError as exc:
            for error in exc.errors():
                location = error["loc"]
                field_name = str(location[0]) if location else "__all__"
                self.errors.setdefault(field_name, []).append(str(error["msg"]))
            return
        self.cleaned_data = self.parsed.model_dump()

    @property
    def is_valid(self) -> bool:
        return self.parsed is not None
