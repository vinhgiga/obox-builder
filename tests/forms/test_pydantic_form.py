from __future__ import annotations

from obox_builder.forms import PydanticForm
from pydantic import BaseModel, ConfigDict, Field


class InviteSchema(BaseModel):
    email: str
    seats: int = Field(ge=1)


def test_valid_form_exposes_parsed_model_and_cleaned_data() -> None:
    form = PydanticForm(InviteSchema, {"email": "team@example.com", "seats": "3"})

    assert form.is_valid
    assert form.errors == {}
    assert form.cleaned_data == {"email": "team@example.com", "seats": 3}
    assert isinstance(form.parsed, InviteSchema)
    assert form.parsed.seats == 3


def test_invalid_form_maps_pydantic_errors_to_fields() -> None:
    form = PydanticForm(InviteSchema, {"email": 3, "seats": 0})

    assert not form.is_valid
    assert form.cleaned_data == {}
    assert set(form.errors) == {"email", "seats"}


def test_form_respects_schema_extra_policy() -> None:
    class StrictSchema(BaseModel):
        model_config = ConfigDict(extra="forbid")
        name: str

    form = PydanticForm(StrictSchema, {"name": "Ada", "is_admin": True})

    assert not form.is_valid
    assert "is_admin" in form.errors
