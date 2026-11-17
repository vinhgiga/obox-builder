import asyncio
from datetime import UTC, date, datetime, time
from decimal import Decimal
from enum import Enum
from typing import Annotated, Any, ClassVar, Literal
from uuid import UUID

import pytest
from obox import forms
from obox.conf import settings
from obox.core.exceptions import ImproperlyConfigured
from pydantic import AfterValidator, BeforeValidator, Field, PlainValidator


class ReviewForm(forms.Form):
    name: str
    rate: int = forms.field(ge=1, le=5)
    note: str | None = forms.field(forms.Textarea, default=None)

    def clean_name(self, value: str) -> str:
        return value.upper()


def validate(form: forms.Form) -> bool:
    return asyncio.run(form.is_valid())


def test_typed_results_require_success_and_are_read_only() -> None:
    form = ReviewForm({"name": " Alice ", "rate": "4", "ignored": "extra"})
    with pytest.raises(RuntimeError, match="successful form validation"):
        _ = form.rate
    assert validate(form)
    assert (form.name, form.rate, form.note) == ("ALICE", 4, None)
    assert type(form.rate) is int
    with pytest.raises(ValueError, match="no field"):
        form.add_error("unknown", "Rejected")
    assert form.rate == 4
    with pytest.raises(AttributeError, match="read-only"):
        form.rate = 3
    form.cleaned_data["rate"] = "unvalidated"
    assert form.rate == 4
    form.add_error(None, "Rejected")
    with pytest.raises(RuntimeError, match="successful form validation"):
        _ = form.name
    assert not validate(form)


@pytest.mark.parametrize("rate", ["1", "5", "3.0"])
def test_integer_boundaries(rate: str) -> None:
    form = ReviewForm({"name": "Alice", "rate": rate})
    assert validate(form)
    assert form.rate == int(Decimal(rate))


@pytest.mark.parametrize(
    ("rate", "code"),
    [("0", "min_value"), ("6", "max_value"), ("1.5", "invalid"), ("bad", "invalid")],
)
def test_integer_errors_preserve_django_codes(rate: str, code: str) -> None:
    form = ReviewForm({"name": "Alice", "rate": rate})
    assert not validate(form)
    assert form.has_error("rate", code)
    assert form.cleaned_data == {"name": "ALICE", "note": None}
    with pytest.raises(RuntimeError):
        _ = form.name


@pytest.mark.parametrize("raw", [{}, {"required": ""}, {"required": "   "}])
def test_required_nullable_and_defaults_distinguish_missing_input(raw: dict[str, str]) -> None:
    class Values(forms.Form):
        required: str
        nullable: str | None
        optional: str | None = None
        title: str = "Draft"

    form = Values(raw)
    assert not validate(form)
    assert form.has_error("required", "required")
    assert form.has_error("nullable", "required")
    assert form.cleaned_data == {"optional": None, "title": "Draft"}
    form = Values({"required": " hello ", "nullable": "   ", "title": "   "})
    assert validate(form)
    assert (form.required, form.nullable, form.optional, form.title) == ("hello", None, None, "")


def test_initial_is_display_only_and_factory_is_cached_per_instance() -> None:
    calls = []

    def default_items() -> list[int]:
        calls.append(1)
        return [1]

    class Defaults(forms.Form):
        name: str = forms.field(initial="Displayed")
        items: list[int] = forms.field(default_factory=default_items)
        tags: list[str] = forms.field(default=[])

    unbound = Defaults()
    assert unbound["name"].value() == "Displayed"
    assert unbound["items"].value() == [1]
    assert unbound["items"].value() == [1]
    assert calls == [1]
    assert not validate(unbound)
    with pytest.raises(RuntimeError):
        _ = unbound.items
    bound = Defaults({}, initial={"name": "Still display only"})
    assert not validate(bound)
    assert bound.has_error("name", "required")
    asyncio.run(bound.full_clean())
    assert calls == [1, 1]
    first, second = Defaults({"name": "one"}), Defaults({"name": "two", "items": "[2]"})
    assert validate(first)
    assert validate(second)
    first.tags.append("changed")
    assert second.tags == []
    assert second.items == [2]
    assert calls == [1, 1, 1]


@pytest.mark.parametrize("value", [None, "", "broken"])
def test_non_nullable_numeric_defaults_do_not_replace_submitted_values(value: Any) -> None:
    class Count(forms.Form):
        count: int = 3

    assert validate(Count({}))
    assert not validate(Count({"count": value}))


def test_defaults_are_validated() -> None:
    class Defaults(forms.Form):
        count: int = forms.field(default_factory=lambda: "bad")

    form = Defaults({})
    assert not validate(form)
    assert form.has_error("count", "invalid")


@pytest.mark.parametrize("result", ["4", 6, None])
def test_custom_field_hook_cannot_break_type_or_constraints(result: Any) -> None:
    class Changed(forms.Form):
        rate: int = forms.field(ge=1, le=5)

        def clean_rate(self, value: int) -> Any:
            assert value == 4
            with pytest.raises(RuntimeError):
                _ = self.rate
            return result

    form = Changed({"rate": "4"})
    assert not validate(form)
    assert form.has_error("rate")
    assert form.cleaned_data == {}


@pytest.mark.parametrize("action", ["wrong_type", "remove", "resurrect"])
def test_form_clean_cannot_publish_invalid_or_missing_values(action: str) -> None:
    class Changed(forms.Form):
        rate: int

        def clean(self) -> dict[str, Any]:
            with pytest.raises(RuntimeError):
                _ = self.rate
            return (
                {"rate": "bad"}
                if action == "wrong_type"
                else ({"rate": 4} if action == "resurrect" else {})
            )

    form = Changed({"rate": "bad" if action == "resurrect" else "4"})
    assert not validate(form)
    assert form.cleaned_data == {}
    assert form.has_error("rate")


def test_concurrent_validation_runs_async_hooks_once_and_hides_pending_values() -> None:
    async def scenario() -> None:
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        class AsyncForm(forms.Form):
            name: str

            async def clean_name(self, value: str) -> str:
                calls.append(value)
                started.set()
                await release.wait()
                return value.upper()

        form = AsyncForm({"name": "Alice"})
        tasks = [asyncio.create_task(form.is_valid()) for _ in range(3)]
        await started.wait()
        with pytest.raises(RuntimeError):
            _ = form.name
        release.set()
        assert await asyncio.gather(*tasks) == [True, True, True]
        assert await form.is_valid()
        assert calls == ["Alice"]
        assert form.name == "ALICE"

    asyncio.run(scenario())


def test_full_clean_overrides_and_explicit_revalidation_are_preserved() -> None:
    calls = []

    class Custom(ReviewForm):
        async def full_clean(self) -> None:
            calls.append(1)
            await super().full_clean()

    async def scenario() -> None:
        form = Custom({"name": "one", "rate": "3"})
        assert await asyncio.gather(form.is_valid(), form.is_valid()) == [True, True]
        assert calls == [1]
        assert form.name == "ONE"
        form.data["name"] = "two"
        await form.full_clean()
        assert form.name == "TWO"
        assert calls == [1, 1]

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel", [False, True])
def test_interrupted_validation_can_retry_without_publishing_partial_values(cancel: bool) -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        attempts = []

        class Interrupted(forms.Form):
            name: str

            async def clean_name(self, value: str) -> str:
                attempts.append(value)
                if len(attempts) == 1:
                    if cancel:
                        started.set()
                        await asyncio.Event().wait()
                    raise RuntimeError("interrupted")
                return value.upper()

        form = Interrupted({"name": "Alice"})
        task = asyncio.create_task(form.is_valid())
        if cancel:
            await started.wait()
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
            await task
        with pytest.raises(RuntimeError):
            _ = form.name
        assert await form.is_valid()
        assert attempts == ["Alice", "Alice"]
        assert form.name == "ALICE"

    asyncio.run(scenario())


def test_annotated_validators_run_once_and_nested_results_are_typed() -> None:
    calls = []

    def double(value: int) -> int:
        calls.append(value)
        return value * 2

    class Values(forms.Form):
        items: list[Annotated[int, AfterValidator(double)]]
        title: Annotated[str, BeforeValidator(lambda value: value.upper()), Field(max_length=5)]

    form = Values({"items": "[2, 3]", "title": "hello"})
    assert validate(form)
    assert form.items == [4, 6]
    assert form.title == "HELLO"
    assert calls == [2, 3]

    class Wrong(forms.Form):
        count: Annotated[int, PlainValidator(lambda value: "bad")]

    assert not validate(Wrong({"count": "1"}))


def test_inheritance_order_overrides_and_instance_isolation() -> None:
    class Parent(forms.Form):
        first: str
        note: str | None = "Original"
        label: ClassVar[str] = "Class setting"

    class Left(Parent):
        pass

    class Right(Parent):
        note: str | None = None

    class Child(Left, Right):
        last: int = 2

    class Required(Parent):
        note: str | None

    first, second = Child({"first": "one"}), Child({"first": "two"})
    assert list(first.fields) == ["first", "note", "last"]
    assert validate(first)
    assert validate(second)
    assert first.note is None
    assert first.label == "Class setting"
    first.fields["first"].widget.attrs["placeholder"] = "one"
    assert "placeholder" not in second.fields["first"].widget.attrs
    assert not validate(Required({"first": "one"}))


@pytest.mark.parametrize(
    "name", ["data", "errors", "fields", "cleaned_data", "is_valid", "initial"]
)
def test_reserved_names_are_rejected(name: str) -> None:
    with pytest.raises(ImproperlyConfigured, match="conflicts"):
        type("Conflicting", (forms.Form,), {"__annotations__": {name: str}})


@pytest.mark.parametrize("annotation", [complex, set[str], int | str, "MissingFormType"])
def test_unsupported_or_unresolved_annotations_are_rejected(annotation: Any) -> None:
    with pytest.raises(ImproperlyConfigured):
        type("Unsupported", (forms.Form,), {"__annotations__": {"value": annotation}})


@pytest.mark.parametrize("declaration", [forms.CharField(), forms.field()])
def test_unannotated_legacy_fields_and_metadata_are_rejected(declaration: Any) -> None:
    with pytest.raises(ImproperlyConfigured, match="annotation"):
        type("Unannotated", (forms.Form,), {"name": declaration})


def test_invalid_declaration_options_are_rejected() -> None:
    with pytest.raises(TypeError, match="not both"):
        forms.field(default=1, default_factory=lambda: 2)
    with pytest.raises(ImproperlyConfigured, match="Widget"):
        forms.field(object())


def test_overrides_require_annotations_and_annotated_legacy_fields_are_rejected() -> None:
    with pytest.raises(ImproperlyConfigured, match="annotation"):
        type("Override", (ReviewForm,), {"rate": 3})
    with pytest.raises(ImproperlyConfigured, match="legacy"):
        type("Legacy", (forms.Form,), {"__annotations__": {"name": str}, "name": forms.CharField()})


def test_future_annotations_and_classvars(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    from types import ModuleType

    module = ModuleType("form_annotations")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(  # noqa: S102 -- fixed fixture source in an isolated module
        "from __future__ import annotations\n"
        "from typing import Annotated, ClassVar\n"
        "from obox.forms import Form, field\n"
        "from pydantic import Field\n"
        "class Future(Form):\n"
        "    config: ClassVar[str] = 'setting'\n"
        "    rate: Annotated[int, Field(ge=1, le=5)]\n"
        "    note: str | None = field(default=None)\n",
        vars(module),
    )
    form = module.Future({"rate": "4"})
    assert validate(form)
    assert form.rate == 4
    assert form.note is None
    assert list(form.fields) == ["rate", "note"]
    assert not validate(module.Future({"rate": "6"}))


def test_boolean_required_and_literal_false_choices() -> None:
    class Consent(forms.Form):
        accepted: bool
        choice: Literal[False]

    form = Consent({"accepted": "on", "choice": "false"})
    assert validate(form)
    assert form.accepted is True
    assert form.choice is False
    assert not validate(Consent({"accepted": "false", "choice": "false"}))


class State(Enum):
    OPEN = "open"
    CLOSED = "closed"


def test_scalar_inference_enum_literal_and_json_widgets(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(settings, "_wrapped", SimpleNamespace(USE_TZ=True, TIME_ZONE="UTC"))

    class Values(forms.Form):
        state: State = State.OPEN
        choice: Literal[1, 2]
        enabled: bool = False
        price: Decimal
        ratio: float
        day: date
        moment: datetime
        clock: time
        identifier: UUID
        payload: dict[str, list[int]]

    identifier = UUID("b090a4ce-4d3e-4c7c-a2a0-5aa3a6ee0a90")
    form = Values(
        {
            "choice": "2",
            "price": "12.50",
            "ratio": "1.5",
            "day": "2026-11-17",
            "moment": "2026-11-17T12:00:00",
            "clock": "12:00:00",
            "identifier": str(identifier),
            "payload": '{"counts": [1, "2"]}',
        }
    )
    assert validate(form)
    assert form.state is State.OPEN
    assert form.choice == 2
    assert form.enabled is False
    assert form.price == Decimal("12.50")
    assert form.ratio == 1.5
    assert form.day == date(2026, 11, 17)
    assert form.moment == datetime(2026, 11, 17, 12, tzinfo=UTC)
    assert form.clock == time(12)
    assert form.identifier == identifier
    assert form.payload == {"counts": [1, 2]}
    assert isinstance(form.fields["state"].widget, forms.Select)
    assert isinstance(form.fields["choice"].widget, forms.Select)
    assert isinstance(form.fields["enabled"].widget, forms.CheckboxInput)
    assert isinstance(form.fields["payload"].widget, forms.Textarea)
    assert 'value="open" selected' in str(Values()["state"])
    assert not validate(Values({**form.data, "state": "unknown"}))
    assert not validate(Values({**form.data, "payload": '{"counts": ["bad"]}'}))


def test_widgets_constraints_disabled_values_and_raw_input_rendering() -> None:
    class Profile(forms.Form):
        name: str = forms.field(
            forms.Textarea(attrs={"rows": 3}),
            label="Display name",
            help_text="<help>",
            max_length=5,
        )
        password: str = forms.field(forms.PasswordInput)
        rate: int = forms.field(ge=1, le=5)
        enabled: bool = forms.field(disabled=True, initial=True, default=False)

    form = Profile(
        {"p-name": "<script>", "p-password": "secret", "p-rate": "9", "p-enabled": "false"},
        prefix="p",
    )
    assert not validate(form)
    assert form.cleaned_data["enabled"] is True
    rendered = str(form.as_p())
    assert "Display name" in rendered
    assert "&lt;script&gt;" in rendered
    assert "&lt;help&gt;" in rendered
    assert "secret" not in rendered
    assert 'rows="3"' in rendered
    assert 'name="p-name"' in rendered
    assert 'value="9"' in str(form["rate"])
    assert 'min="1"' in rendered
    assert 'max="5"' in rendered
    assert "checked" in str(form["enabled"])


def test_schema_adapters_are_not_rebuilt_during_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected_adapter(*args: Any, **kwargs: Any) -> None:
        pytest.fail("The class schema must be reused across form instances.")

    monkeypatch.setattr("obox.forms.declarative.TypeAdapter", unexpected_adapter)
    assert validate(ReviewForm({"name": "one", "rate": "2"}))
    assert validate(ReviewForm({"name": "two", "rate": "3"}))
