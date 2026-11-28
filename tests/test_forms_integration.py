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
from obox.core.exceptions import ResourceConflict
from obox.core.principal import ANONYMOUS
from obox.db.engine import close_engines, default_session_factory
from obox.db.session import session_scope
from obox.db.transaction import atomic
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from tests.runtime import resource_for


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


ItemResource = resource_for(Item, write_schema=ItemInput)


class ItemForm(forms.Form[ItemInput]):
    schema = ItemInput
    field_options: ClassVar[dict[str, Any]] = {"name": forms.Field(label="Title")}


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
        assert missing.errors == {"name": ["Field required"]}
        valid = Contact({"name": " ok ", "unknown": "ignored"})
        assert await valid.is_valid()
        assert valid.cleaned_data is not None
        assert valid.cleaned_data.model_dump() == {"name": "OK", "age": None}
        rejected = Contact({"name": "bad", "age": "1"})
        assert not await rejected.is_valid()
        assert "Rejected" in rejected.errors["name"][0]
        assert rejected.cleaned_data is None
        first, second = Contact(), Contact()
        first.fields["name"].required = False
        assert second.fields["name"].required
        assert 'name="name"' in "".join(str(field) for field in first)

    asyncio.run(scenario())


def test_form_validation_and_resource_write() -> None:
    async def scenario() -> None:
        async with configured(), session_scope() as session:
            resource = ItemResource(session, ANONYMOUS)
            form = ItemForm({"name": " ALPHA ", "price": "2.50"})
            assert await form.is_valid()
            assert form.cleaned_data is not None
            assert form.cleaned_data.name == "alpha"
            assert form.fields["name"].label == "Title"
            assert await session.scalar(sa.select(sa.func.count()).select_from(Item)) == 0
            record = await resource.create(await form.input_data())
            assert record.price == Decimal("2.50")
            assert record.id == 1
            with pytest.raises(ResourceConflict):
                await resource.create({"name": "alpha", "price": "1.00"})
            await resource.update(record, {"price": "3.00"})
            assert record.price == Decimal("3.00")

    asyncio.run(scenario())


def test_atomic_rolls_back_form_writes() -> None:
    async def scenario() -> None:
        async with configured():
            with pytest.raises(RuntimeError, match="rollback"):  # noqa: PT012 - transaction rollback
                async with atomic(), session_scope() as session:
                    form = ItemForm({"name": "new", "price": "1.00"})
                    await ItemResource(session, ANONYMOUS).create(await form.input_data())
                    raise RuntimeError("rollback")
            async with session_scope() as session:
                assert await session.scalar(sa.select(sa.func.count()).select_from(Item)) == 0

    asyncio.run(scenario())


class ManualKey(Base):
    __tablename__ = "v01_manual_keys"
    code: Mapped[str] = mapped_column(sa.String(12), primary_key=True)
    label: Mapped[str] = mapped_column(sa.String(40))


def test_manual_primary_key_uniqueness_and_validation_preserves_instance() -> None:
    async def scenario() -> None:
        async with configured(), session_scope() as session:
            resource = resource_for(ManualKey)(session, ANONYMOUS)
            obj = await resource.create({"code": "key", "label": "Original"})
            with pytest.raises(ResourceConflict):
                await resource.create({"code": "key", "label": "Other"})
            assert await session.scalar(sa.select(ManualKey.label)) == "Original"
            assert await resource.update(obj, {"label": "Changed"}) is obj
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
        field_options: ClassVar[dict[str, Any]] = {
            "name": forms.Field(widget=forms.Textarea),
            "password": forms.Field(widget=forms.PasswordInput),
            "accepted": forms.BooleanField(disabled=True, initial=True, required=False),
        }

    async def scenario() -> None:
        form = Profile({"p-name": "<script>", "p-password": "secret"}, prefix="p")
        assert await form.is_valid()
        assert form.cleaned_data is not None
        assert form.cleaned_data.accepted is True
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
        assert not form.has_error("name", "missing")
        assert form.cleaned_data is None

    asyncio.run(scenario())


def test_atomic_task_isolation_savepoint_and_durable_contract() -> None:
    async def scenario() -> None:
        async with configured(), session_scope() as parent:

            async def child() -> Any:
                async with atomic(), session_scope() as session:
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

    async def scenario() -> None:
        async with configured(), session_scope() as session:
            resource = resource_for(Choice)(session, ANONYMOUS)
            assert (await resource.create({"state": "open"})).state is State.OPEN
            with pytest.raises(ResourceConflict):
                await resource.create({"state": "open"})

    asyncio.run(scenario())
