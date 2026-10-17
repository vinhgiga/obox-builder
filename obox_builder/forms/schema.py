"""Build Pydantic models from field specs (the validation engine)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, create_model

from obox_builder.fields.base import FieldSpec


def build_schema(
    specs: Sequence[FieldSpec],
    *,
    name: str = "OboxForm",
    mode: str = "add",
) -> type[BaseModel]:
    """Create a Pydantic model validating exactly the editable fields.

    Read-only and secret fields never appear, so mass assignment through them
    is impossible. ``extra='forbid'`` rejects unknown keys outright.
    """
    fields: dict[str, Any] = {}
    for spec in specs:
        if not spec.is_editable():
            continue
        annotation = spec.type.annotation(spec)
        default: Any = ... if spec.required else None
        fields[spec.name] = (annotation, default)
    model = create_model(
        f"{name}_{mode.title()}",
        __config__=ConfigDict(extra="forbid"),
        **fields,
    )
    return model
