import asyncio
from datetime import datetime
from decimal import Decimal
from io import BytesIO
from typing import Annotated, Any, ClassVar, Literal
from uuid import UUID

import pytest
from obox import forms
from obox.core.exceptions import ImproperlyConfigured
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator
from starlette.datastructures import FormData, UploadFile


class ReviewInput(BaseModel):
    name: str = Field(min_length=1)
    rate: int = Field(ge=1, le=5)
    note: str | None = None

    @field_validator("name")
    @classmethod
    def normalize(cls, value: str) -> str:
        return value.strip().upper()


class ReviewForm(forms.Form[ReviewInput]):
    schema = ReviewInput
    widgets: ClassVar[dict[str, Any]] = {"note": forms.Textarea}


def validate(form: forms.Form[Any]) -> bool:
    return asyncio.run(form.is_valid())


def test_bound_unbound_typed_result_errors_and_cache() -> None:
    form = ReviewForm({"name": " Alice ", "rate": "4", "ignored": "extra"})
    assert form.cleaned_data is None
    assert validate(form)
    assert isinstance(form.cleaned_data, ReviewInput)
    assert (form.cleaned_data.name, form.cleaned_data.rate, form.cleaned_data.note) == (
        "ALICE",
        4,
        None,
    )
    assert form["name"].value() == " Alice "
    assert validate(form)
    with pytest.raises(ValueError, match="no field"):
        form.add_error("unknown", "Rejected")
    form.add_error(None, "Rejected")
    assert form.cleaned_data is None
    assert not validate(form)
    assert form.non_field_errors() == ["Rejected"]
    assert not validate(ReviewForm())


@pytest.mark.parametrize("rate", ["1", "5", "3.0"])
def test_numeric_boundaries(rate: str) -> None:
    form = ReviewForm({"name": "Alice", "rate": rate})
    assert validate(form)
    assert form.cleaned_data is not None
    assert form.cleaned_data.rate == int(Decimal(rate))


@pytest.mark.parametrize("rate", ["0", "6", "1.5", "bad", ""])
def test_errors_preserve_raw_values(rate: str) -> None:
    form = ReviewForm({"name": "Alice", "rate": rate})
    assert not validate(form)
    assert form.errors_for("rate")
    assert form.cleaned_data is None
    assert form.value("rate") == rate


def test_validation_error_locations_codes_and_cross_field_errors() -> None:
    class Input(BaseModel):
        name: str
        quantity: int = Field(ge=1)

        @model_validator(mode="after")
        def check_total(self) -> "Input":
            if self.name == "reserved":
                raise ValueError("Choose another name.")
            return self

    invalid = forms.Form({"quantity": "0"}, schema=Input)
    assert not validate(invalid)
    assert invalid.has_error("name", "missing")
    assert invalid.has_error("quantity", "greater_than_equal")
    reserved = forms.Form({"name": "reserved", "quantity": "1"}, schema=Input)
    assert not validate(reserved)
    assert "Choose another name" in reserved.non_field_errors()[0]


def test_missing_blank_nullable_and_default_values_are_distinct() -> None:
    class Input(BaseModel):
        required: str = Field(min_length=1)
        nullable: int | None
        title: str = "Draft"
        description: str = ""

    form = forms.Form({"required": "value", "nullable": "", "description": ""}, schema=Input)
    assert validate(form)
    assert form.cleaned_data is not None
    assert form.cleaned_data.model_dump() == {
        "required": "value",
        "nullable": None,
        "title": "Draft",
        "description": "",
    }
    assert not validate(forms.Form({"required": "", "nullable": ""}, schema=Input))
    missing = forms.Form({"required": "value"}, schema=Input)
    assert not validate(missing)
    assert missing.has_error("nullable", "missing")


def test_defaults_are_cached_per_instance_and_initial_is_display_only() -> None:
    calls = []

    def default_items() -> list[int]:
        calls.append(1)
        return [1]

    class Input(BaseModel):
        name: str
        items: list[int] = Field(default_factory=default_items)

    form = forms.Form({}, schema=Input, initial={"name": "Displayed"})
    assert form["name"].value() is None
    assert form["items"].value() is None
    assert not validate(form)
    assert calls == [1]
    unbound = forms.Form(schema=Input)
    assert unbound["items"].value() == [1]
    assert unbound["items"].value() == [1]
    assert calls == [1, 1]


def test_schema_validators_run_once_with_concurrent_validation() -> None:
    calls = []

    def double(value: int) -> int:
        calls.append(value)
        return value * 2

    class Input(BaseModel):
        items: list[Annotated[int, AfterValidator(double)]]

    async def scenario() -> None:
        form = forms.Form(FormData([("items", "2"), ("items", "3")]), schema=Input)
        assert await asyncio.gather(form.is_valid(), form.is_valid()) == [True, True]
        assert form.cleaned_data is not None
        assert form.cleaned_data.items == [4, 6]
        assert calls == [2, 3]

    asyncio.run(scenario())


def test_failed_validator_can_be_retried_without_caching_programming_errors() -> None:
    calls = []

    class Input(BaseModel):
        value: str

        @field_validator("value")
        @classmethod
        def check(cls, value: str) -> str:
            calls.append(value)
            if len(calls) == 1:
                raise RuntimeError("temporary failure")
            return value

    form = forms.Form({"value": "ok"}, schema=Input)
    with pytest.raises(RuntimeError, match="temporary"):
        validate(form)
    assert validate(form)
    assert calls == ["ok", "ok"]


def test_prefix_checkbox_multiple_values_and_html_escaping() -> None:
    class Input(BaseModel):
        name: str
        enabled: bool
        tags: list[int]
        password: str

    class Profile(forms.Form[Input]):
        schema = Input
        widgets: ClassVar[dict[str, Any]] = {"password": forms.PasswordInput}
        labels: ClassVar[dict[str, Any]] = {"name": "<Name>"}
        help_texts: ClassVar[dict[str, Any]] = {"name": "<Help>"}

    raw = FormData(
        [("p-name", "<script>"), ("p-tags", "1"), ("p-tags", "2"), ("p-password", " secret ")]
    )
    form = Profile(raw, prefix="p")
    assert validate(form)
    assert form.cleaned_data is not None
    assert form.cleaned_data.enabled is False
    assert form.cleaned_data.tags == [1, 2]
    assert form.cleaned_data.password == raw["p-password"]
    assert "&lt;script&gt;" in "".join(str(field) for field in form)
    assert form["name"].label == "<Name>"
    assert form.fields["name"].help_text == "<Help>"
    assert "secret" not in str(form["password"])
    assert 'name="p-name"' in str(form["name"])
    second = Profile()
    form.fields["name"].widget.attrs["placeholder"] = "first"
    assert "placeholder" not in second.fields["name"].widget.attrs


def test_disabled_fields_use_trusted_initial_and_do_not_change() -> None:
    class Input(BaseModel):
        name: str
        enabled: bool = False

    class Profile(forms.Form[Input]):
        schema = Input
        field_options: ClassVar[dict[str, Any]] = {
            "enabled": forms.BooleanField(disabled=True, initial=True, required=False)
        }

    form = Profile({"name": "new", "enabled": "false"})
    assert validate(form)
    assert form.cleaned_data is not None
    assert form.cleaned_data.enabled is True
    assert "enabled" not in form.changed_data
    assert "checked" in str(form["enabled"])


def test_literal_choices_decimal_uuid_and_datetime_controls() -> None:
    class Input(BaseModel):
        status: Literal["draft", "live"]
        price: Decimal = Field(gt=0, max_digits=5, decimal_places=2)
        key: UUID
        at: datetime

    raw = {
        "status": "draft",
        "price": "1.25",
        "key": "c48d380f-e0f0-4d84-b3e7-6619f070c0c1",
        "at": "2026-11-23T09:00",
    }
    form = forms.Form(raw, schema=Input)
    assert validate(form)
    assert form.cleaned_data is not None
    assert form.cleaned_data.price == Decimal("1.25")
    assert "<select" in str(form["status"])
    invalid = forms.Form({**raw, "status": "other", "price": "0"}, schema=Input)
    assert not validate(invalid)
    assert {"status", "price"} <= invalid.errors.keys()


@pytest.mark.parametrize(("size", "valid"), [(3, True), (4, False)])
def test_multipart_upload_size_and_stream_lifetime(size: int, valid: bool) -> None:
    class Input(BaseModel):
        model_config = ConfigDict(arbitrary_types_allowed=True)
        upload: UploadFile

    class Upload(forms.Form[Input]):
        schema = Input
        widgets: ClassVar[dict[str, Any]] = {"upload": forms.FileInput(max_size=3)}

    stream = BytesIO(b"x" * size)
    upload = UploadFile(stream, filename="file.bin", size=size)
    form = Upload(FormData([("upload", upload)]), schema=Input)
    assert form.is_multipart()
    assert validate(form) is valid
    assert not stream.closed
    if valid:
        assert form.cleaned_data is not None
        assert form.cleaned_data.upload is upload
    else:
        assert form.has_error("upload", "max_size")


def test_empty_form_and_configuration_errors() -> None:
    assert validate(forms.Form({}, schema=type("EmptyInput", (BaseModel,), {})))
    with pytest.raises(ImproperlyConfigured, match="schema"):
        type("Broken", (forms.Form,), {"schema": str})()


@pytest.mark.parametrize(
    ("raw", "expected"), [({}, False), ({"enabled": "on"}, True), ({"enabled": "false"}, False)]
)
def test_unchecked_checkbox_overrides_schema_default(raw: dict[str, str], expected: bool) -> None:
    class Input(BaseModel):
        enabled: bool = True

    form = forms.Form(raw, schema=Input)
    assert validate(form)
    assert form.cleaned_data is not None
    assert form.cleaned_data.enabled is expected


def test_schema_error_locations_are_preserved_and_control_metadata_survives_binding() -> None:
    class Input(BaseModel):
        values: list[int]

    class Numbers(forms.Form[Input]):
        schema = Input
        field_options: ClassVar[dict[str, Any]] = {"values": forms.JSONField(widget=forms.Textarea)}

    form = Numbers(FormData([("values", '[1, "invalid"]')]), schema=Input)
    assert isinstance(form.fields["values"].widget, forms.Textarea)
    assert not validate(form)
    assert form.error_details[0]["loc"] == ("values", 1)
    assert form.errors_for("values")
