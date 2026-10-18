"""The append-only ``RowChange`` table.

Rows are immutable: nothing in the framework ever updates or deletes them
(purge/retention arrives in a later phase). ``id`` is a monotonic identity
column, so entries sort by time without UUIDv7.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from obox_builder.core.utils.time import utcnow
from obox_builder.db import Model
from obox_builder.db.types import JSONB, BigIntIdentity, JsonDict, TZDateTime

OPS = ("create", "update", "delete")
SOURCES = ("admin_ui", "cli", "job", "api", "system", "revert", "db_trigger")


class RowChange(Model):
    """One captured CREATE/UPDATE/DELETE for a tracked row."""

    app_label = "history"

    id: Mapped[int] = mapped_column(BigIntIdentity, primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow, index=True)
    change_set_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, index=True)
    op: Mapped[str] = mapped_column(sa.String(16))
    model_label: Mapped[str] = mapped_column(sa.String(150), index=True)
    object_pk: Mapped[str] = mapped_column(sa.String(255))
    object_repr: Mapped[str] = mapped_column(sa.String(255), default="", server_default="")
    changes: Mapped[JsonDict] = mapped_column(JSONB, default=dict)
    schema_version: Mapped[int] = mapped_column(default=1)
    row_version: Mapped[int | None] = mapped_column(default=None)
    actor_id: Mapped[int | None] = mapped_column(sa.BigInteger, default=None)
    actor_label: Mapped[str] = mapped_column(
        sa.String(150), default="system", server_default="system"
    )
    source: Mapped[str] = mapped_column(sa.String(16), default="system", server_default="system")
    request_id: Mapped[str | None] = mapped_column(sa.String(64), default=None)
    ip: Mapped[str | None] = mapped_column(sa.String(45), default=None)
    user_agent: Mapped[str | None] = mapped_column(sa.String(255), default=None)
    reason: Mapped[str | None] = mapped_column(sa.String(500), default=None)
    reverts_id: Mapped[int | None] = mapped_column(BigIntIdentity, default=None)

    __table_args__ = (
        sa.CheckConstraint("op IN ('create', 'update', 'delete')", name="op"),
        sa.CheckConstraint(
            "source IN ('admin_ui', 'cli', 'job', 'api', 'system', 'revert', 'db_trigger')",
            name="source",
        ),
        sa.Index(
            "ix_history_row_change_model_object",
            "model_label",
            "object_pk",
            "id",
        ),
    )

    def __repr__(self) -> str:
        return f"<RowChange {self.op} {self.model_label}#{self.object_pk}>"


class SealBlock(Model):
    """A hash-chain segment over a contiguous ``id`` range of entries.

    ``history_seal`` builds one row per sealed batch (entries older than the
    lag window, in ``id`` order); ``history_verify`` recomputes every leaf and
    re-derives ``merkle_root``/``block_hash`` to detect edits, deletes and
    chain rewrites. Hashes are never written back into entries.
    """

    app_label = "history"

    block_id: Mapped[int] = mapped_column(BigIntIdentity, primary_key=True)
    first_id: Mapped[int] = mapped_column(sa.BigInteger, index=True)
    last_id: Mapped[int] = mapped_column(sa.BigInteger)
    count: Mapped[int] = mapped_column(sa.Integer)
    merkle_root: Mapped[str] = mapped_column(sa.String(64))
    prev_block_hash: Mapped[str | None] = mapped_column(sa.String(64), default=None)
    block_hash: Mapped[str] = mapped_column(sa.String(64))
    sealed_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)

    def __repr__(self) -> str:
        return f"<SealBlock {self.block_id} [{self.first_id}..{self.last_id}]>"
