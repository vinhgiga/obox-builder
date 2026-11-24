import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any, ClassVar
from uuid import uuid4

import pytest
import sqlalchemy as sa
from obox import forms
from obox.conf import settings
from obox.core.exceptions import ImproperlyConfigured
from obox.db.engine import close_engines, default_session_factory
from obox.db.session import session_scope
from obox.db.transaction import atomic
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Item(Base):
    __tablename__ = "v01_form_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(10), unique=True)
    price: Mapped[Decimal] = mapped_column(sa.Numeric(6, 2))
    quantity: Mapped[int] = mapped_column(default=1)


class ItemInput(BaseModel):
    name: str = Field(min_length=1, max_length=10)
    price: Decimal
    quantity: int = 1

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return value.strip().lower()


class ItemForm(forms.ModelForm[ItemInput]):
    schema = ItemInput

    class Meta:
        model = Item
        fields = ("name", "price", "quantity")
        labels: ClassVar[dict[str, str]] = {"name": "Title"}


@asynccontextmanager
async def configured() -> AsyncIterator[None]:
    from obox.apps import Apps, apps

    registry = vars(apps).copy()
    vars(apps).clear()
    vars(apps).update(vars(Apps()))
    previous = settings._wrapped
    settings._wrapped = None
    config: dict[str, Any] = {"ENGINE": "obox.db.backends.sqlite3", "NAME": ":memory:"}
    url = os.environ.get("OBOX_TEST_DATABASE_URL", "")
    schema, provisioner = None, None
    if url.startswith("postgresql"):
        parsed = make_url(url)
        schema = "core_" + uuid4().hex
        provisioner = create_async_engine(url)
        async with provisioner.begin() as connection:
            await connection.execute(sa.schema.CreateSchema(schema))
        config = {
            "ENGINE": "obox.db.backends.postgresql",
            "NAME": parsed.database,
            "USER": parsed.username,
            "PASSWORD": parsed.password,
            "HOST": parsed.host,
            "PORT": parsed.port,
            "OPTIONS": {"server_settings": {"search_path": schema}},
        }
    settings.configure(
        DATABASES={"default": config},
        SECRET_KEY="s" * 32,
    )
    try:
        engine = default_session_factory().kw["bind"]
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield
    finally:
        await close_engines()
        if provisioner is not None:
            async with provisioner.begin() as connection:
                await connection.execute(sa.schema.DropSchema(schema, cascade=True))
            await provisioner.dispose()
        settings._wrapped = previous
        vars(apps).clear()
        vars(apps).update(registry)


def test_form_bound_validation_order_errors_and_per_instance_fields() -> None:
    class ContactInput(BaseModel):
        name: str = Field(min_length=1, max_length=5)
        age: int | None = None

        @field_validator("name")
        @classmethod
        def clean_name(cls, value: str) -> str:
            if value == "bad":
                raise ValueError("Rejected")
            return value.strip().upper()

        @model_validator(mode="after")
        def check_age(self) -> "ContactInput":
            if self.age == 1:
                raise ValueError("Too young")
            return self

    class Contact(forms.Form[ContactInput]):
        schema = ContactInput

    async def scenario() -> None:
        assert not await Contact().is_valid()
        missing = Contact({})
        assert not await missing.is_valid()
        assert missing.errors == {"name": ["This field is required."]}
        valid = Contact({"name": " ok ", "unknown": "ignored"})
        assert await valid.is_valid()
        assert valid.validated_data == {"name": "OK", "age": None}
        rejected = Contact({"name": "bad", "age": "1"})
        assert not await rejected.is_valid()
        assert "Rejected" in rejected.errors["name"][0]
        assert rejected.validated_data == {}
        first, second = Contact(), Contact()
        first.fields["name"].required = False
        assert second.fields["name"].required
        assert 'name="name"' in str(first.as_p())

    asyncio.run(scenario())


def test_model_form_validates_without_saving_and_supports_commit_false() -> None:
    async def scenario() -> None:
        async with configured():
            form = ItemForm({"name": " ALPHA ", "price": "2.50"})
            assert await form.is_valid()
            assert form.validated_data["name"] == "alpha"
            assert form.fields["name"].label == "Title"
            async with session_scope() as session:
                assert await session.scalar(sa.select(sa.func.count()).select_from(Item)) == 0
            record = await form.save(commit=False)
            assert record.id is None
            assert record.price == Decimal("2.50")
            await form.save()
            assert record.id == 1
            duplicate = ItemForm({"name": "alpha", "price": "1.00"})
            assert not await duplicate.is_valid()
            assert "already exists" in duplicate.errors["name"][0]
            with pytest.raises(ValueError, match="could not be saved"):
                await duplicate.save()
            edited = ItemForm({"name": "alpha", "price": "3.00"}, instance=record)
            assert await edited.is_valid()
            await edited.save()
            assert record.price == Decimal("3.00")

    asyncio.run(scenario())


def test_model_form_requires_explicit_field_configuration() -> None:
    with pytest.raises(ImproperlyConfigured):

        class Unsafe(forms.ModelForm):
            class Meta:
                model = Item


def test_model_form_preserves_legacy_declarations_overrides_and_clean_hooks() -> None:
    class Custom(ItemForm):
        name = forms.CharField(max_length=4)
        extra = forms.CharField(required=False)

    class Removed(Custom):
        extra = None

    async def scenario() -> None:
        async with configured():
            form = Custom({"name": " ABC ", "price": "2.00", "extra": "kept"})
            assert await form.is_valid()
            assert form.validated_data["name"] == "abc"
            assert form.validated_data["extra"] == "kept"
            assert "extra" not in Removed.base_fields
            assert not await Custom({"name": "ABCDE", "price": "2.00"}).is_valid()

    asyncio.run(scenario())


def test_atomic_rolls_back_form_writes() -> None:
    async def scenario() -> None:
        async with configured():
            with pytest.raises(RuntimeError, match="rollback"):  # noqa: PT012 - transaction rollback
                async with atomic():
                    form = ItemForm({"name": "new", "price": "1.00"})
                    assert await form.is_valid()
                    await form.save()
                    raise RuntimeError("rollback")
            async with session_scope() as session:
                assert await session.scalar(sa.select(sa.func.count()).select_from(Item)) == 0

    asyncio.run(scenario())


class ManualKey(Base):
    __tablename__ = "v01_manual_keys"
    code: Mapped[str] = mapped_column(sa.String(12), primary_key=True)
    label: Mapped[str] = mapped_column(sa.String(40))


def test_manual_primary_key_uniqueness_and_validation_preserves_instance() -> None:
    class KeyForm(forms.ModelForm):
        class Meta:
            model = ManualKey
            fields = ("code", "label")

    async def scenario() -> None:
        async with configured():
            form = KeyForm({"code": "key", "label": "Original"})
            assert await form.is_valid()
            obj = await form.save()
            duplicate = KeyForm({"code": "key", "label": "Other"})
            assert not await duplicate.is_valid()
            edited = KeyForm({"code": "key", "label": "Changed"}, instance=obj)
            async with atomic():
                assert await edited.is_valid()
                assert obj.label == "Original"
                async with session_scope() as session:
                    assert await session.scalar(sa.select(ManualKey.label)) == "Original"
                await edited.save()
            assert obj.label == "Changed"

    asyncio.run(scenario())


def test_prefix_disabled_fields_error_codes_and_widgets_escape_values() -> None:
    validated = []

    class ProfileInput(BaseModel):
        name: str
        accepted: bool = False
        password: str

        @field_validator("accepted")
        @classmethod
        def track(cls, value: bool) -> bool:
            validated.append(value)
            return value

    class Profile(forms.Form[ProfileInput]):
        schema = ProfileInput
        widgets: ClassVar[dict[str, Any]] = {
            "name": forms.Textarea,
            "password": forms.PasswordInput,
        }
        field_options: ClassVar[dict[str, Any]] = {
            "accepted": forms.BooleanField(disabled=True, initial=True, required=False)
        }

    async def scenario() -> None:
        form = Profile({"p-name": "<script>", "p-password": "secret"}, prefix="p")
        assert await form.is_valid()
        assert form.validated_data["accepted"] is True
        assert validated == [True]

        class PayloadInput(BaseModel):
            payload: dict[str, str]

        class Payload(forms.Form[PayloadInput]):
            schema = PayloadInput

        rendered = str(Payload(initial={"payload": {"key": "value"}})["payload"])
        assert "&quot;key&quot;: &quot;value&quot;" in rendered
        assert "&quot;value&quot;" in str(Payload(initial={"payload": "value"})["payload"])
        assert 'name="p-name"' in str(form["name"])
        assert "&lt;script&gt;" in str(form["name"])
        assert "secret" not in str(form["password"])
        assert "checked" in str(form["accepted"])
        form.add_error("name", forms.ValidationError("Reserved", code="reserved"))
        assert form.has_error("name", "reserved")
        assert not form.has_error("name", "required")
        assert "name" not in form.validated_data

    asyncio.run(scenario())


def test_atomic_task_isolation_savepoint_and_durable_contract() -> None:
    async def scenario() -> None:
        async with configured(), session_scope() as parent:

            async def child() -> Any:
                async with atomic(durable=True), session_scope() as session:
                    return session

            assert await asyncio.create_task(child()) is not parent
            parent.add(Item(name="kept", price=Decimal("1")))
            await parent.flush()
            with pytest.raises(RuntimeError, match="rollback"):  # noqa: PT012
                async with atomic():
                    parent.add(Item(name="rolled", price=Decimal("2")))
                    await parent.flush()
                    raise RuntimeError("rollback")
            assert (await parent.scalars(sa.select(Item.name))).all() == ["kept"]
            with pytest.raises(RuntimeError, match="durable"):
                async with atomic(durable=True):
                    pass

    asyncio.run(scenario())


def test_enum_model_values_are_normalized_for_persistence_and_unique_validation() -> None:
    from enum import Enum

    class State(Enum):
        OPEN = "open"
        CLOSED = "closed"

    class Choice(Base):
        __tablename__ = "v01_choices"
        id: Mapped[int] = mapped_column(primary_key=True)
        state: Mapped[State] = mapped_column(sa.Enum(State), unique=True)

    class ChoiceForm(forms.ModelForm):
        class Meta:
            model = Choice
            fields = ("state",)

    async def scenario() -> None:
        async with configured():
            form = ChoiceForm({"state": "open"})
            assert await form.is_valid()
            assert (await form.save()).state is State.OPEN
            assert not await ChoiceForm({"state": "open"}).is_valid()

    asyncio.run(scenario())
