from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa
from obox_builder.exceptions import ImproperlyConfigured, PermissionDenied
from obox_builder.resources import (
    ModelResource,
    OperationContext,
    Resource,
    ResourcePolicy,
    ResourceRegistry,
    ResourceValidationError,
    capability,
    operation,
)
from obox_builder.security import Actor
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Record(Base):
    __tablename__ = "resource_record"
    _model_label = "example"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int]
    name: Mapped[str] = mapped_column(sa.String(80), unique=True)
    payload: Mapped[str] = mapped_column(default="private")
    internal_note: Mapped[str] = mapped_column(default="never published")


class Owned(ResourcePolicy):
    def scope(self, ctx, query, operation):
        return query.where(Record.owner_id == ctx.actor.id)


class Records(ModelResource[Record]):
    name = "example.record"
    model = Record
    fields = ("id", "name", "payload")
    read_payload = capability("Can read private payloads")
    fields_config = {"payload": {"read_permission": read_payload}}
    policy = Owned()


class Input(BaseModel):
    amount: int


class Output(BaseModel):
    amount: int


class Counter(Resource):
    name = "example.counter"

    @operation(name="increment", label="Can increment counters")
    async def increment_impl(self, ctx: OperationContext, data: Input) -> Output:
        return Output(amount=data.amount + 1)


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        session.add_all(
            [
                Record(id=1, owner_id=7, name="visible"),
                Record(id=2, owner_id=8, name="hidden"),
            ]
        )
        await session.commit()
        yield session
    await engine.dispose()


def context(db=None, *, permissions=(), scopes=None, registry=None):
    return OperationContext(
        Actor(id=7, permissions=frozenset(permissions)),
        db=db,
        scopes=scopes,
        registry=registry,
    )


async def test_direct_python_calls_enforce_grants_and_validate_io():
    resource = Counter()
    with pytest.raises(PermissionDenied):
        await resource.increment_impl(context(), {"amount": 1})
    actor = context(permissions={"example.counter.increment"})
    assert await resource.increment_impl(actor, {"amount": 4}) == Output(amount=5)
    with pytest.raises(ResourceValidationError):
        await resource.increment_impl(actor, {"amount": "invalid"})


async def test_method_override_retains_stable_identity_and_authorization():
    class Changed(Counter):
        async def increment_impl(self, ctx: OperationContext, data: Input) -> Output:
            return Output(amount=data.amount + 2)

    resource = Changed()
    assert set(resource.operations) == {"increment"}
    assert resource.increment_impl.permission.name == "example.counter.increment"
    with pytest.raises(PermissionDenied):
        await resource.increment_impl(context(), Input(amount=1))
    assert (
        await resource.increment_impl(
            context(permissions={"example.counter.increment"}), Input(amount=1)
        )
    ).amount == 3


async def test_scopes_restrict_known_grants_even_for_superuser():
    actor = Actor(id=7, is_superuser=True, permissions=frozenset({"example.counter.increment"}))
    resource = Counter()
    with pytest.raises(PermissionDenied):
        await resource.increment_impl(
            OperationContext(actor, scopes=("example.counter:read",)), Input(amount=1)
        )
    result = await resource.increment_impl(
        OperationContext(actor, scopes=("example.counter:write",)), Input(amount=1)
    )
    assert result.amount == 2


async def test_query_scope_field_capability_and_no_implicit_fields(db):
    registry = ResourceRegistry([Records])
    resource = registry.get("example.record")
    ctx = context(db, permissions={"example.record.read"}, registry=registry)
    result = await resource.list(ctx, {})
    assert [item.name for item in result.items] == ["visible"]
    assert result.items[0].model_dump(exclude_unset=True) == {"id": "1", "name": "visible"}
    assert "internal_note" not in resource.read_schema.model_fields
    with pytest.raises(LookupError):
        await resource.get(ctx, target=2)
    ctx = context(db, permissions={"example.record.read", "example.record.readPayload"})
    assert (await resource.get(ctx, target=1)).payload == "private"


async def test_updates_reject_unknown_readonly_and_out_of_scope_records(db):
    resource = Records()
    ctx = context(db, permissions={"example.record.update"})
    for data in ({"internal_note": "changed"}, {"id": 2}):
        with pytest.raises(ResourceValidationError):
            await resource.update(ctx, data, target=1)
    with pytest.raises(LookupError):
        await resource.update(ctx, {"name": "changed"}, target=2)
    result = await resource.update(ctx, {"name": "changed"}, target=1)
    assert result.name == "changed"
    assert (await db.get(Record, 1)).internal_note == "never published"


def test_registry_is_immutable_and_rejects_duplicate_owners():
    registry = ResourceRegistry([Counter, Counter])
    assert set(registry.permissions) == {"example.counter.increment"}
    with pytest.raises(TypeError):
        registry.permissions["anything"] = None

    class Other(Counter):
        pass

    with pytest.raises(ImproperlyConfigured, match="Duplicate resource"):
        ResourceRegistry([Counter, Other])


def test_disabled_operations_do_not_create_or_expose_permissions():
    class Restricted(Records):
        exclude_operations = (
            "create",
            "update",
            "delete",
            "deleteSelected",
            "bulkCreate",
            "bulkUpdate",
        )

    resource = Restricted()
    assert set(resource.operations) == {"list", "get"}
    assert set(resource.permissions) == {"example.record.read", "example.record.readPayload"}


def test_registration_rejects_missing_fields_and_untyped_custom_operations():
    class Unpublished(Records):
        fields = ()

    with pytest.raises(ImproperlyConfigured, match="fields"):
        ResourceRegistry([Unpublished])

    class Invalid(Resource):
        name = "example.invalid"

        @operation()
        async def calculate(self, ctx, data: int) -> int:
            return data

    with pytest.raises(ImproperlyConfigured, match="Pydantic"):
        ResourceRegistry([Invalid])
