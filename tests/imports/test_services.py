from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from obox_builder.conf import settings
from obox_builder.contrib.history import track, use
from obox_builder.contrib.history.models import RowChange
from obox_builder.contrib.imports import ImportResource, services
from obox_builder.contrib.imports.models import ImportRow, ImportSession
from obox_builder.contrib.imports.parser import ImportFileError
from obox_builder.core.exceptions import ImproperlyConfigured, PermissionDenied
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support.apps.shop.models import Category, Order, Product


def resource(
    model: type[Any] = Order, *, keys: tuple[str, ...] = ("reference",), **kwargs: Any
) -> ImportResource:
    user = SimpleNamespace(id=1, is_active=True, has_perm=lambda perm: True)
    return ImportResource(model, user, keys=keys, **kwargs)


async def stage(
    db: AsyncSession,
    policy: ImportResource,
    content: bytes,
    *,
    mode: str = "insert",
    mapping: dict[str, str] | None = None,
) -> ImportSession:
    session = await services.create_import(db, policy, "data.csv", content, mode=mode)
    services.inspect_file(session)
    session.sheet = "Data"
    await services.parse_file(db, session)
    await services.set_mapping(
        db, session, policy, mapping or services.suggest_mapping(session.headers, policy)
    )
    while await services.validate_batch(db, session, policy):
        await db.flush()
    await db.commit()
    return session


async def apply(db: AsyncSession, session: ImportSession, policy: ImportResource) -> None:
    services.confirm_import(session, policy)
    await db.commit()
    while await services.commit_batch(db, session, policy):
        await db.commit()
    await db.commit()


@pytest.mark.parametrize(
    "mode,expected,errors,skipped",
    [
        ("insert", ["old", "new"], 1, 0),
        ("upsert", ["old", "new"], 0, 0),
        ("update-only", ["old"], 1, 0),
        ("skip-existing", ["old", "new"], 0, 1),
    ],
)
async def test_all_write_modes(
    db: AsyncSession, mode: str, expected: list[str], errors: int, skipped: int
) -> None:
    db.add(Order(reference="old"))
    await db.commit()
    policy = resource()
    session = await stage(db, policy, b"reference\nold\nnew\n", mode=mode)
    counts = await services.summary(db, session)
    assert counts.get("error", 0) == errors
    assert counts.get("skip", 0) == skipped
    await apply(db, session, policy)
    assert sorted(await db.scalars(sa.select(Order.reference))) == sorted(expected)
    assert session.status == "completed"


async def test_update_clears_mapped_blanks_and_preserves_unmapped_values(db: AsyncSession) -> None:
    category = Category(name="original")
    db.add(category)
    await db.flush()
    product = Product(
        name="before", status="live", price_cents=42, category_id=category.id, is_active=True
    )
    db.add(product)
    await db.commit()
    policy = resource(Product, keys=("id",))
    session = await stage(
        db, policy, f"id,name,category_id\n{product.id},after,\n".encode(), mode="update-only"
    )
    await apply(db, session, policy)
    await db.refresh(product)
    assert (
        product.name,
        product.status,
        product.price_cents,
        product.category_id,
        product.is_active,
    ) == ("after", "live", 42, None, True)


async def test_insert_omitted_defaults_and_primary_key_generation(db: AsyncSession) -> None:
    policy = resource(Product, keys=("id",))
    session = await stage(db, policy, b"name,status\nnew,draft\n")
    await apply(db, session, policy)
    product = await db.scalar(sa.select(Product))
    assert product is not None
    assert product.id
    assert product.is_active
    assert product.meta == {}
    assert product.price_cents == 0


@pytest.mark.parametrize("mode", services.MODES)
async def test_duplicate_keys_reject_every_occurrence(db: AsyncSession, mode: str) -> None:
    session = await stage(db, resource(), b"reference\nrepeat\nrepeat\n", mode=mode)
    assert await services.summary(db, session) == {"error": 2}
    await apply(db, session, resource())
    assert await db.scalar(sa.select(sa.func.count()).select_from(Order)) == 0


async def test_conflict_after_preview_does_not_overwrite(db: AsyncSession) -> None:
    product = Product(name="before")
    db.add(product)
    await db.commit()
    policy = resource(Product, keys=("id",))
    session = await stage(
        db, policy, f"id,name\n{product.id},imported\n".encode(), mode="update-only"
    )
    product.name = "edited by someone else"
    await db.commit()
    await apply(db, session, policy)
    await db.refresh(product)
    assert product.name == "edited by someone else"
    row = await db.scalar(sa.select(ImportRow).where(ImportRow.session_id == session.id))
    assert row is not None
    assert "after preview" in str(row.errors)
    assert not row.committed


async def test_database_conflict_after_preview_skips_only_bad_row(db: AsyncSession) -> None:
    policy = resource(keys=())
    session = await stage(db, policy, b"reference\ntaken\ngood\n")
    db.add(Order(reference="taken"))
    await db.commit()
    await apply(db, session, policy)
    assert sorted(await db.scalars(sa.select(Order.reference))) == ["good", "taken"]
    assert (await services.summary(db, session))["error"] == 1


async def test_committed_batch_retry_and_uncommitted_batch_rollback(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(services, "BATCH_SIZE", 1)
    policy = resource()
    session = await stage(db, policy, b"reference\none\ntwo\n")
    session.status = "committing"
    await db.commit()
    session_id = session.id
    assert await services.commit_batch(db, session, policy)
    await db.rollback()
    reloaded = await db.get(ImportSession, session_id)
    assert reloaded is not None
    session = reloaded
    assert await db.scalar(sa.select(sa.func.count()).select_from(Order)) == 0
    assert await services.commit_batch(db, session, policy)
    await db.commit()
    assert await services.commit_batch(db, session, policy)
    await db.commit()
    assert not await services.commit_batch(db, session, policy)
    await db.commit()
    assert sorted(await db.scalars(sa.select(Order.reference))) == ["one", "two"]
    assert (await services.summary(db, session))["insert_done"] == 2


async def test_preview_has_no_history_and_commit_uses_existing_capture(db: AsyncSession) -> None:
    track(Order)
    session = await stage(db, resource(), b"reference\naudit\n")
    assert await db.scalar(sa.select(sa.func.count()).select_from(RowChange)) == 0
    async with use(source="job", actor_id=1, reason=f"Import {session.id}"):
        await apply(db, session, resource())
    entry = await db.scalar(sa.select(RowChange).where(RowChange.model_label == "shop.order"))
    assert entry is not None
    assert entry.source == "job"
    assert entry.actor_id == 1


async def test_row_scope_and_object_permissions_are_enforced(db: AsyncSession) -> None:
    db.add(Order(reference="private"))
    await db.commit()

    class Restricted(ImportResource):
        def queryset(self) -> sa.Select[Any]:
            return sa.select(Order).where(Order.reference != "private")

    policy = Restricted(Order, resource().user, keys=("reference",))
    session = await stage(db, policy, b"reference\nprivate\n", mode="upsert")
    assert (await services.summary(db, session))["error"] == 1
    policy.can_change = lambda obj=None: obj is None  # type: ignore[method-assign]
    session = await stage(db, policy, b"reference\nprivate\n", mode="update-only")
    assert (await services.summary(db, session))["error"] == 1


async def test_mapping_and_permissions_fail_closed(db: AsyncSession) -> None:
    policy = resource()
    with pytest.raises(ImproperlyConfigured, match="unique"):
        resource(Product, keys=("name",)).check_configuration("upsert")
    with pytest.raises(ImproperlyConfigured, match="keys"):
        resource(keys=()).check_configuration("upsert")
    session = await services.create_import(db, policy, "data.csv", b"reference,extra\na,b\n")
    services.inspect_file(session)
    session.sheet = "Data"
    await services.parse_file(db, session)
    for mapping in (
        {"0": "reference", "1": "reference"},
        {"2": "reference"},
        {"0": "created_at"},
        {},
    ):
        with pytest.raises(ValueError, match="Map|mapping"):
            await services.set_mapping(db, session, policy, mapping)
    policy.user.id = 2
    with pytest.raises(PermissionDenied):
        await services.set_mapping(db, session, policy, {"0": "reference"})
    policy.user.id = 1
    policy.user.has_perm = lambda perm: False
    with pytest.raises(PermissionDenied):
        await services.set_mapping(db, session, policy, {"0": "reference"})


async def test_conditional_readonly_and_validation_reused(db: AsyncSession) -> None:
    base = resource()
    specs = [
        replace(spec, editable_if=lambda ctx: False) if spec.name == "reference" else spec
        for spec in base.specs
    ]
    policy = resource(specs=specs)
    session = await stage(db, policy, b"reference\nblocked\n")
    assert (await services.summary(db, session))["error"] == 1


async def test_long_blank_gap_is_not_silently_truncated(db: AsyncSession) -> None:
    policy = resource()
    session = await stage(db, policy, b"reference\nfirst\n" + b"\n" * 1001 + b"last\n")
    rows = list(
        await db.scalars(
            sa.select(ImportRow)
            .where(ImportRow.session_id == session.id)
            .order_by(ImportRow.number)
        )
    )
    assert [row.number for row in rows] == [2, 1004]


async def test_row_limits_and_malformed_rows(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.load(), "IMPORT_MAX_ROWS", 1)
    with pytest.raises(ImportFileError, match="row limit"):
        await stage(db, resource(), b"reference\na\nb\n")
    await db.rollback()
    with pytest.raises(ImportFileError, match="more values"):
        await stage(db, resource(), b"reference\na,b\n")


async def test_upsert_updates_existing_and_inserts_missing_id(db: AsyncSession) -> None:
    product = Product(name="old", status="draft")
    db.add(product)
    await db.commit()
    policy = resource(Product, keys=("id",))
    session = await stage(
        db,
        policy,
        f"id,name,status\n{product.id},updated,live\n,new,draft\n".encode(),
        mode="upsert",
    )
    assert await services.summary(db, session) == {"insert": 1, "update": 1}
    services.confirm_import(session, policy)
    await db.commit()
    while await services.commit_batch(db, session, policy):
        await db.commit()
    await db.commit()
    assert sorted(await db.scalars(sa.select(Product.name))) == ["new", "updated"]


async def test_invalid_values_and_nonnullable_clears_are_row_errors(db: AsyncSession) -> None:
    product = Product(name="before", status="draft")
    db.add(product)
    await db.commit()
    policy = resource(Product, keys=("id",))
    session = await stage(
        db, policy, f"id,price_cents\n{product.id},\n".encode(), mode="update-only"
    )
    assert (await services.summary(db, session))["error"] == 1
    session = await stage(db, resource(), ("reference\n" + "a" * 41 + "\n").encode())
    assert (await services.summary(db, session))["error"] == 1


async def test_object_permission_denial_and_configuration_change(db: AsyncSession) -> None:
    db.add(Order(reference="visible"))
    await db.commit()
    policy = resource()
    policy.can_change = lambda obj=None: obj is None  # type: ignore[method-assign]
    session = await stage(db, policy, b"reference\nvisible\n", mode="update-only")
    assert (await services.summary(db, session))["error"] == 1
    session = await stage(db, resource(), b"reference\nnew\n")
    session.resource_signature = "changed"
    with pytest.raises(ValueError, match="mapping"):
        services.confirm_import(session, resource())


async def test_expiry_purges_source_and_staging(db: AsyncSession) -> None:
    from datetime import timedelta

    from obox_builder.contrib.imports.management.commands.imports_purge import handle
    from obox_builder.core.utils.time import utcnow

    session = await stage(db, resource(), b"reference\nexpired\n")
    session.created_at = utcnow() - timedelta(days=8)
    session_id = session.id
    await db.commit()
    await handle.func()
    db.expunge_all()
    assert await db.get(ImportSession, session_id) is None
    assert await db.scalar(sa.select(sa.func.count()).select_from(ImportRow)) == 0


async def test_python_service_rollback_without_jobs_or_events(
    imports_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from obox_builder.db import dispose, get_sessionmaker

    monkeypatch.setattr(settings.load(), "JOBS_ENABLED", False)
    monkeypatch.setattr(settings.load(), "EVENTS_ENABLED", False)
    await dispose()
    async with get_sessionmaker()() as db:
        policy = resource()
        session = await stage(db, policy, b"reference\nrollback\n")
        services.confirm_import(session, policy)
        session.status = "committing"
        await db.commit()
        assert await services.commit_batch(db, session, policy)
        await db.rollback()
        assert await db.scalar(sa.select(sa.func.count()).select_from(Order)) == 0


async def test_out_of_range_integer_skips_only_affected_row(db: AsyncSession) -> None:
    policy = resource(Product, keys=("id",))
    session = await stage(
        db, policy, f"name,status,price_cents\nbad,draft,{2**100}\ngood,draft,42\n".encode()
    )
    await apply(db, session, policy)
    assert list(await db.scalars(sa.select(Product.name))) == ["good"]
    assert (await services.summary(db, session))["error"] == 1


async def test_generated_ids_cannot_be_assigned_to_new_records(db: AsyncSession) -> None:
    policy = resource(Product, keys=("id",))
    session = await stage(
        db, policy, b"id,name,status\n1,explicit,draft\n,generated,draft\n", mode="upsert"
    )
    assert await services.summary(db, session) == {"error": 1, "insert": 1}
    await apply(db, session, policy)
    db.add(Product(name="ordinary create"))
    await db.commit()
    assert sorted(await db.scalars(sa.select(Product.name))) == ["generated", "ordinary create"]
