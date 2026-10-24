from __future__ import annotations

# Field validation cases are adapted from Django's forms_tests/field_tests suite.
import pytest
from obox_builder.fields import FieldSpec, field_types
from obox_builder.forms import BoundForm, build_schema


def url_spec(**type_options: object) -> FieldSpec:
    return FieldSpec(name="url", label="URL", type=field_types.create("url", **type_options))


def field_spec(
    type_name: str, *, required: bool = False, nullable: bool = True, **options: object
) -> FieldSpec:
    return FieldSpec(
        name="value",
        label="Value",
        type=field_types.create(type_name),
        required=required,
        nullable=nullable,
        options=options,
    )


@pytest.mark.parametrize(
    "value",
    [
        "http://localhost",
        "https://localhost",
        "https://example.com/path?query=value",
        "HTTP://example.com",
        "http://例子.测试/",
    ],
)
def test_url_type_accepts_valid_http_urls(value: str) -> None:
    field_type = field_types.create("url")
    assert field_type.validate(value, url_spec()) is None


@pytest.mark.parametrize(
    "value",
    [
        "http://",
        "https://",
        "https://erp",
        "http://example",
        "ftp://example.com",
        "javascript:alert(1)",
        "http://example.com:bad",
        "http://[a",
        f"http://{'x' * 200}",
    ],
)
def test_url_type_rejects_invalid_http_urls(value: str) -> None:
    field_type = field_types.create("url")
    assert field_type.validate(value, url_spec()) == "Enter a valid URL (http:// or https://)."


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("example.com", "https://example.com"),
        ("  example.com  ", "https://example.com"),
        ("localhost", "https://localhost"),
        ("//example.com/path", "https://example.com/path"),
        ("http://example.com", "http://example.com"),
    ],
)
def test_url_form_assumes_https_and_keeps_explicit_scheme(value: str, expected: str) -> None:
    spec = url_spec()
    bound = BoundForm.bind([spec], build_schema([spec]), {"url": value})
    assert bound.is_valid
    assert bound.cleaned["url"] == expected


def test_url_form_supports_custom_assume_scheme() -> None:
    spec = url_spec(assume_scheme="http")
    bound = BoundForm.bind([spec], build_schema([spec]), {"url": "example.com"})
    assert bound.is_valid
    assert bound.cleaned["url"] == "http://example.com"


@pytest.mark.parametrize("value", ["erp", "http://erp", "https://erp"])
def test_url_form_rejects_non_localhost_single_label_hosts(value: str) -> None:
    spec = url_spec()
    bound = BoundForm.bind([spec], build_schema([spec]), {"url": value})
    assert not bound.is_valid
    assert bound.errors["url"] == ["Enter a valid URL (http:// or https://)."]


@pytest.mark.parametrize("value", ["on", "true", "1", "yes", "y"])
def test_checkbox_accepts_html_true_values(value: str) -> None:
    spec = field_spec("checkbox")
    assert spec.type.from_input(value, spec) is True


def test_checkbox_treats_missing_required_value_as_false() -> None:
    spec = field_spec("checkbox", nullable=False)
    assert spec.type.from_input(None, spec) is False


def test_choice_field_rejects_values_outside_its_choices() -> None:
    spec = field_spec("single_select", choices=[("draft", "Draft"), ("live", "Live")])
    assert spec.type.validate(spec.type.from_input("live", spec), spec) is None
    assert spec.type.validate(spec.type.from_input("admin", spec), spec) == (
        "Select a valid choice."
    )


def test_email_field_trims_and_checks_basic_address_shape() -> None:
    spec = field_spec("email")
    value = spec.type.from_input("  person@example.com  ", spec)
    assert value == "person@example.com"
    assert spec.type.validate(value, spec) is None
    assert spec.type.validate("invalid", spec) == "Enter a valid email address."


@pytest.mark.parametrize("value", ["NaN", "Infinity"])
def test_decimal_form_rejects_non_finite_values(value: str) -> None:
    spec = field_spec("decimal")
    bound = BoundForm.bind([spec], build_schema([spec]), {"value": value})
    assert not bound.is_valid
    assert bound.errors["value"]


def test_date_form_rejects_impossible_calendar_date() -> None:
    spec = field_spec("date")
    invalid = BoundForm.bind([spec], build_schema([spec]), {"value": "2026-02-30"})
    valid = BoundForm.bind([spec], build_schema([spec]), {"value": "2024-02-29"})
    assert not invalid.is_valid
    assert invalid.errors["value"]
    assert valid.is_valid


def test_json_form_parses_input_and_reports_invalid_json() -> None:
    spec = field_spec("json")
    valid = BoundForm.bind([spec], build_schema([spec]), {"value": '{"enabled": true}'})
    invalid = BoundForm.bind([spec], build_schema([spec]), {"value": "{"})
    assert valid.is_valid
    assert valid.cleaned["value"] == {"enabled": True}
    assert not invalid.is_valid
    assert invalid.errors["value"] == ["Enter valid JSON."]


def test_json_form_reports_byte_limit() -> None:
    spec = field_spec("json", max_bytes=8)
    bound = BoundForm.bind([spec], build_schema([spec]), {"value": '{"a": 123}'})
    assert not bound.is_valid
    assert bound.errors["value"] == ["JSON must be 8 bytes or smaller."]
