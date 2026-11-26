"""Regression coverage for request isolation and public builder configuration."""

import asyncio
from types import SimpleNamespace
from typing import Any, ClassVar, cast

import pytest
from obox.api import ModelApi
from obox.core.exceptions import ImproperlyConfigured, NotFound, ValidationError
from obox.core.principal import Principal
from obox.forms import CharField, Form, ModelForm
from obox.forms.models import fields_for_model
from obox.resources import ModelResource
from obox.views.generic.edit import FormMixin
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Example(Base):
    __tablename__ = "hardening_examples"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str]


class Examples(ModelApi[Example]):
    model = Example
    prefix = "/examples"
    include_router_methods = ("list",)
    search_fields = ("title",)


def test_api_resource_cache_is_scoped_to_principal() -> None:
    api = Examples()
    api._resource = ModelResource(model=Example)
    session = SimpleNamespace(info={})
    first = Principal(id=1, is_authenticated=True)
    second = Principal(id=2, is_authenticated=True)
    first_context = SimpleNamespace(session=session, principal=first)
    second_context = SimpleNamespace(session=session, principal=second)

    bound = api.get_resource(cast(Any, first_context))
    assert bound is api.get_resource(cast(Any, first_context))
    assert bound.principal is first
    other = api.get_resource(cast(Any, second_context))
    assert other is not bound
    assert other.principal is second
    assert api.get_resource(cast(Any, first_context)).principal is first


@pytest.mark.parametrize(
    ("attribute", "value", "message"),
    [
        ("include_router_methods", "list", "sequence"),
        ("include_router_methods", ("list", "bad"), "supported"),
        ("include_router_methods", ("list", "list"), "unique"),
        ("permissions", {"bad": "x.y"}, "permission"),
        ("permissions", {"create": "invalid"}, "permission"),
        ("include_router_methods", ("list", []), "supported"),
        ("default_limit", True, "page limits"),
        ("max_limit", 100.5, "page limits"),
        ("search_fields", "title", "sequences"),
        ("ordering_fields", "title", "sequences"),
    ],
)
def test_api_rejects_invalid_declarations(attribute: str, value: Any, message: str) -> None:
    api = Examples()
    setattr(api, attribute, value)
    with pytest.raises(ImproperlyConfigured, match=message):
        api.build_router()


def test_model_fields_require_explicit_sequences() -> None:
    with pytest.raises(TypeError, match="fields must be a sequence"):
        fields_for_model(Example, fields="title")
    with pytest.raises(TypeError, match="exclude must be a sequence"):
        fields_for_model(Example, exclude="title")
    with pytest.raises(ImproperlyConfigured, match="unique"):
        fields_for_model(Example, fields=("title", "title"))


def test_inherited_declared_model_form_fields_are_not_mutated() -> None:
    class Parent(ModelForm[Any]):
        title = CharField()

        class Meta:
            model = Example
            fields = ("title",)
            labels: ClassVar[dict[str, str]] = {"title": "Original"}

    class Child(Parent):
        class Meta(Parent.Meta):
            labels: ClassVar[dict[str, str]] = {"title": "Changed"}

    assert Parent.base_fields["title"].label == "Original"
    assert Child.base_fields["title"].label == "Changed"
    assert Parent.base_fields["title"] is not Child.base_fields["title"]


def test_view_initial_values_are_isolated() -> None:
    class ExampleFormMixin(FormMixin):
        initial: ClassVar[dict[str, Any]] = {"config": {"tags": ["first"]}}

    view = ExampleFormMixin()
    first = view.get_initial()
    first["config"]["tags"].append("second")
    assert view.get_initial() == {"config": {"tags": ["first"]}}
    assert ExampleFormMixin.initial == {"config": {"tags": ["first"]}}


def test_schema_forms_run_explicit_field_validators_once() -> None:
    calls: list[str] = []

    class Input(BaseModel):
        name: str | None = None

    def validate(value: str) -> None:
        calls.append(value)
        if value == "blocked":
            raise ValidationError("Disallowed value", code="invalid_choice")

    class Profile(Form[Input]):
        schema = Input
        field_options: ClassVar[dict[str, CharField]] = {
            "name": CharField(required=True, validators=[validate])
        }

    async def scenario() -> None:
        absent = Profile({})
        assert not await absent.is_valid()
        assert absent.has_error("name", "required")
        valid = Profile({"name": "allowed"})
        assert await valid.is_valid()
        assert await valid.is_valid()
        assert calls == ["allowed"]
        denied = Profile({"name": "blocked"})
        assert not await denied.is_valid()
        assert denied.has_error("name", "invalid_choice")
        assert calls == ["allowed", "blocked"]

    asyncio.run(scenario())


def test_schema_forms_preserve_strict_numeric_validation() -> None:
    class FieldStrictInput(BaseModel):
        value: int = Field(strict=True)

    class ModelStrictInput(BaseModel):
        model_config = ConfigDict(strict=True)
        value: int

    class LaxOverrideInput(BaseModel):
        model_config = ConfigDict(strict=True)
        value: int = Field(strict=False)

    async def scenario() -> None:
        for schema in (FieldStrictInput, ModelStrictInput):
            form = Form({"value": "7"}, schema=schema)
            assert not await form.is_valid()
            assert form.has_error("value", "invalid")

            typed = Form({"value": 7}, schema=schema)
            assert await typed.is_valid()

        overridden = Form({"value": "7"}, schema=LaxOverrideInput)
        assert await overridden.is_valid()
        assert overridden.cleaned_data is not None
        assert overridden.cleaned_data.value == 7

    asyncio.run(scenario())


def test_base_resource_binding_reuses_compiled_metadata_without_shared_identity() -> None:
    source = ModelResource(model=Example)
    first = Principal(id=1)
    second = Principal(id=2)
    first_session, second_session = object(), object()
    bound_first = source.bind(cast(Any, first_session), first)
    bound_second = source.bind(cast(Any, second_session), second)

    assert bound_first is not source
    assert bound_second is not source
    assert source.session is None
    assert bound_first.principal is first
    assert bound_second.principal is second
    assert bound_first.session is first_session
    assert bound_second.session is second_session
    assert bound_first.specs is source.specs
    assert bound_second.get_schema("create") is source.get_schema("create")


def test_subclass_resource_bind_keeps_custom_initialization() -> None:
    class Custom(ModelResource[Example]):
        model = Example
        calls = 0

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            type(self).calls += 1

    source = Custom()
    bound = source.bind(cast(Any, object()), Principal())
    assert isinstance(bound, Custom)
    assert Custom.calls == 2


@pytest.mark.parametrize("invalid", [-1, True, 2.5, "2"])
def test_pagination_rejects_invalid_values(invalid: Any) -> None:
    from obox.core.pagination import ListResult, Page

    with pytest.raises(ValueError, match="Invalid result window"):
        ListResult([], offset=invalid)
    with pytest.raises(ValueError, match="Invalid page number"):
        Page([], number=1, page_size=invalid, total=0)


def test_callable_form_initial_is_evaluated_once_per_instance() -> None:
    calls: list[int] = []

    def next_initial() -> str:
        calls.append(len(calls))
        return str(len(calls))

    class Profile(Form[Any]):
        field_options: ClassVar[dict[str, CharField]] = {
            "name": CharField(required=False, initial=next_initial)
        }

    first, second = Profile(), Profile()
    assert first["name"].value() == "1"
    assert first["name"].value() == "1"
    assert second["name"].value() == "2"
    assert calls == [0, 1]


def test_json_form_fields_preserve_empty_containers() -> None:
    class Input(BaseModel):
        payload: dict[str, int]
        tags: list[int]

    form = Form({"payload": "{}", "tags": "[]"}, schema=Input)
    assert asyncio.run(form.is_valid())
    assert form.cleaned_data is not None
    assert form.cleaned_data.payload == {}
    assert form.cleaned_data.tags == []


@pytest.mark.parametrize("invalid", [True, -1, 1.5, "5"])
def test_resource_rejects_invalid_list_windows(invalid: Any) -> None:
    resource = ModelResource(model=Example)
    with pytest.raises(ValueError, match="positive limit"):
        asyncio.run(resource.list(offset=invalid))
    with pytest.raises(ValueError, match="positive limit"):
        asyncio.run(resource.list(limit=invalid))


@pytest.mark.parametrize("invalid", [True, -1, 0, 1.5, "2"])
def test_resource_rejects_invalid_page_numbers(invalid: Any) -> None:
    resource = ModelResource(model=Example)
    with pytest.raises(NotFound):
        asyncio.run(resource.paginate(number=invalid))


@pytest.mark.parametrize("invalid", [True, -1, 0, 1.5, "2"])
def test_resource_rejects_invalid_page_sizes(invalid: Any) -> None:
    resource = ModelResource(model=Example)
    with pytest.raises(ImproperlyConfigured):
        asyncio.run(resource.paginate(page_size=invalid))
