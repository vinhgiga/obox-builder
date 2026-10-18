"""Auth models in the audit trail: identity changes tracked, secrets redacted."""

from __future__ import annotations

import sqlalchemy as sa
from obox_builder.contrib.history.models import RowChange
from obox_builder.contrib.history.service import model_label
from sqlalchemy.ext.asyncio import AsyncSession


async def rows_for(db: AsyncSession, obj: object) -> list[RowChange]:
    stmt = (
        sa.select(RowChange)
        .where(
            RowChange.model_label == model_label(type(obj)),
            RowChange.object_pk == str(obj.id),  # type: ignore[attr-defined]
        )
        .order_by(RowChange.id)
    )
    return list(await db.scalars(stmt))


async def test_user_creation_is_tracked_without_password_material(db: AsyncSession) -> None:
    from obox_builder.contrib.auth import services

    user = await services.create_user(db, "Vinh", "s3cure-password", full_name="Vinh N.")
    await db.commit()

    rows = await rows_for(db, user)
    assert [row.op for row in rows] == ["create", "update"]
    create = rows[0]
    assert create.model_label == "auth.user"
    assert create.changes["fields"]["username"] == [None, "vinh"]
    assert create.changes["fields"]["full_name"] == [None, "Vinh N."]
    assert "password_hash" not in create.changes["fields"]
    assert "meta" not in create.changes["fields"]  # excluded JSON blob

    password_update = rows[1]
    assert password_update.changes["redacted"] == ["password_hash"]
    assert password_update.changes["fields"] == {}
    assert "s3cure-password" not in str(password_update.changes)


async def test_login_bookkeeping_is_noise_free(db: AsyncSession) -> None:
    from obox_builder.contrib.auth import services

    await services.create_user(db, "vinh", "s3cure-password")
    await db.commit()

    await services.authenticate(db, "vinh", "wrong-password")
    await db.commit()
    assert await services.authenticate(db, "vinh", "s3cure-password") is not None
    await db.commit()

    user = await services.get_user_by_username(db, "vinh")
    assert user is not None
    rows = await rows_for(db, user)
    # Only the create/update pair from create_user: throttling counters,
    # timestamps and lock state stay out of the audit trail.
    assert [row.op for row in rows] == ["create", "update"]


async def test_role_and_its_flags_are_tracked(db: AsyncSession) -> None:
    from obox_builder.contrib.auth.models import Role

    role = Role(name="ops", description="Operations")
    db.add(role)
    await db.commit()
    role.is_system = True
    await db.commit()

    rows = await rows_for(db, role)
    assert [row.op for row in rows] == ["create", "update"]
    assert rows[0].model_label == "auth.role"
    assert rows[0].changes["fields"]["name"] == [None, "ops"]
    assert rows[1].changes["fields"] == {"is_system": [False, True]}


async def test_user_delete_snapshots_identity(db: AsyncSession) -> None:
    from obox_builder.contrib.auth import services
    from obox_builder.contrib.auth.models import User

    user = await services.create_user(db, "gone", "s3cure-password")
    await db.commit()
    user_id = user.id
    user = await db.get(User, user_id)
    assert user is not None
    await db.delete(user)
    await db.commit()

    rows = [row for row in await rows_for(db, user) if row.op == "delete"]
    [delete] = rows
    assert delete.object_repr == "gone"
    assert delete.changes["fields"]["username"] == ["gone", None]
    assert "password_hash" not in delete.changes["fields"]
    assert delete.changes["redacted"] == []
