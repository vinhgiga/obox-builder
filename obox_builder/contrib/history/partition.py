"""``history_partition``: manage monthly RANGE partitions of ``history_row_change``.

PostgreSQL only. The conversion from a plain table to a partitioned one is
*printed* (``history_partition --sql``) rather than executed by the CLI: it is a
maintenance-window operation that rearranges a live, append-only table, so an
operator reviews and applies it (see ``docs/history.md``). Once partitioned:

* ``ensure`` creates the coming months plus a ``DEFAULT`` partition, so inserts
  never fail when the cron drifts;
* ``drop_before`` applies retention with ``DROP TABLE`` on whole month
  partitions (O(1), no bloat), never touching ``history_row_change_default``.

Partition names are ``history_row_change_YYYYMM``; only names this module minted
are ever dropped.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("obox.history.audit")

TABLE = "history_row_change"
DEFAULT_PARTITION = f"{TABLE}_default"
_BOUND_TO_RE = re.compile(r"TO \('([^']+)'\)")
_OFFSET_RE = re.compile(r"([+-]\d{2})$")


class PartitionError(RuntimeError):
    """Raised when partitioning is requested on an unsupported database/table."""


@dataclass
class PartitionInfo:
    """One existing partition (``upper`` is ``None`` when it must never be
    dropped: the DEFAULT partition, ``MAXVALUE`` bounds or an unknown shape)."""

    name: str
    upper: datetime | None

    @property
    def is_default(self) -> bool:
        return self.upper is None


@dataclass
class PartitionResult:
    """What an ``ensure``/``drop_before`` run did (or would do)."""

    created: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    rows_dropped: int = 0
    dry_run: bool = False


def month_start(moment: datetime) -> datetime:
    return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def add_months(moment: datetime, count: int) -> datetime:
    month = moment.month - 1 + count
    year = moment.year + month // 12
    return moment.replace(year=year, month=month % 12 + 1, day=1)


def partition_name(moment: datetime) -> str:
    return f"{TABLE}_{moment.year:04d}{moment.month:02d}"


def parse_month(value: str) -> datetime:
    match = re.fullmatch(r"(\d{4})-(\d{2})", value)
    if not match or not 1 <= int(match.group(2)) <= 12:
        raise PartitionError(f"Invalid month {value!r}; expected YYYY-MM.")
    return datetime(int(match.group(1)), int(match.group(2)), 1, tzinfo=UTC)


# -- catalog ------------------------------------------------------------------


async def _relkind(db: AsyncSession) -> str | None:
    kind = await db.scalar(
        sa.text("SELECT relkind FROM pg_class WHERE oid = to_regclass(:name)"), {"name": TABLE}
    )
    return None if kind is None else str(kind)


def _parse_upper(bound: str) -> datetime | None:
    """Upper bound of one ``pg_get_expr(relpartbound)`` value, if it has one."""
    match = _BOUND_TO_RE.search(bound)
    if not match:
        return None  # DEFAULT, MAXVALUE or an unknown shape: never auto-dropped
    stamp = _OFFSET_RE.sub(r"\1:00", match.group(1))
    try:
        return datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S%z")
    except ValueError:
        logger.warning("history_partition: cannot parse partition bound %r.", bound)
        return None


async def partition_infos(db: AsyncSession) -> list[PartitionInfo]:
    """Partitions of ``history_row_change``; raises unless it is partitioned."""
    if db.get_bind().dialect.name != "postgresql":
        raise PartitionError("history_partition requires PostgreSQL.")
    kind = await _relkind(db)
    if kind is None:
        raise PartitionError(f"Table {TABLE} does not exist.")
    if kind != "p":
        raise PartitionError(
            f"{TABLE} is not partitioned yet. Review and apply `history_partition --sql` "
            "(see docs/history.md, Partitioning on PostgreSQL)."
        )
    rows = await db.execute(
        sa.text(
            "SELECT c.relname, pg_get_expr(c.relpartbound, c.oid) FROM pg_class c "
            "JOIN pg_inherits i ON i.inhrelid = c.oid "
            "WHERE i.inhparent = to_regclass(:name) ORDER BY c.relname"
        ),
        {"name": TABLE},
    )
    return [PartitionInfo(name, _parse_upper(bound)) for name, bound in rows]


# -- ensure / retention -------------------------------------------------------


async def ensure(
    db: AsyncSession,
    *,
    months: int = 2,
    now: datetime | None = None,
    dry_run: bool = False,
) -> PartitionResult:
    """Create the current + next ``months - 1`` partitions and the DEFAULT one."""
    if months < 1:
        raise PartitionError("months must be >= 1.")
    infos = await partition_infos(db)
    existing = {info.name for info in infos}
    result = PartitionResult(dry_run=dry_run)
    base = month_start(now or datetime.now(UTC))
    for offset in range(months):
        name = partition_name(add_months(base, offset))
        if name in existing:
            result.kept.append(name)
            continue
        lower = add_months(base, offset).strftime("%Y-%m-%d %H:%M:%S+00:00")
        upper = add_months(base, offset + 1).strftime("%Y-%m-%d %H:%M:%S+00:00")
        logger.info("history_partition: create %s [%s, %s).", name, lower, upper)
        if not dry_run:
            await db.execute(
                sa.text(
                    f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF {TABLE} "
                    f"FOR VALUES FROM ('{lower}') TO ('{upper}')"
                )
            )
        result.created.append(name)
    if DEFAULT_PARTITION not in existing:
        logger.info("history_partition: create DEFAULT partition %s.", DEFAULT_PARTITION)
        if not dry_run:
            await db.execute(
                sa.text(
                    f"CREATE TABLE IF NOT EXISTS {DEFAULT_PARTITION} PARTITION OF {TABLE} DEFAULT"
                )
            )
        result.created.append(DEFAULT_PARTITION)
    else:
        result.kept.append(DEFAULT_PARTITION)
    if not dry_run:
        await db.commit()
    return result


async def drop_before(
    db: AsyncSession,
    *,
    month: str,
    dry_run: bool = False,
) -> PartitionResult:
    """Retention: drop whole month partitions ending before ``month`` (YYYY-MM)."""
    cutoff = parse_month(month)
    infos = await partition_infos(db)
    result = PartitionResult(dry_run=dry_run)
    for info in infos:
        if info.is_default or info.upper is None or info.upper > cutoff:
            result.kept.append(info.name)
            continue
        # Identifier is safe: partition_infos only returns our own names
        # (NAME_RE) or the DEFAULT partition; unmanaged ones are ignored.
        count = int(
            await db.scalar(sa.text(f"SELECT count(*) FROM {info.name}")) or 0  # noqa: S608
        )
        logger.warning(
            "history_partition: drop %s (retention before %s, %d row(s)).",
            info.name,
            month,
            count,
        )
        if not dry_run:
            await db.execute(sa.text(f"DROP TABLE {info.name}"))
        result.dropped.append(info.name)
        result.rows_dropped += count
    if not dry_run:
        await db.commit()
    return result


# -- conversion recipe --------------------------------------------------------

_LEGACY_INDEXES = (
    "ix_history_row_change_model_object",
    "ix_history_row_change_occurred_at",
    "ix_history_row_change_model_label",
    "ix_history_row_change_change_set_id",
)

_COLUMNS = """
    id BIGINT NOT NULL DEFAULT nextval('history_row_change_id_seq'),
    occurred_at TIMESTAMP WITH TIME ZONE NOT NULL,
    change_set_id UUID NOT NULL,
    op VARCHAR(16) NOT NULL,
    model_label VARCHAR(150) NOT NULL,
    object_pk VARCHAR(255) NOT NULL,
    object_repr VARCHAR(255) DEFAULT '' NOT NULL,
    changes JSONB NOT NULL,
    schema_version INTEGER NOT NULL,
    row_version INTEGER,
    actor_id BIGINT,
    actor_label VARCHAR(150) DEFAULT 'system' NOT NULL,
    source VARCHAR(16) DEFAULT 'system' NOT NULL,
    request_id VARCHAR(64),
    ip VARCHAR(45),
    user_agent VARCHAR(255),
    reason VARCHAR(500),
    reverts_id BIGINT
"""


def conversion_sql(cutoff: datetime, *, concurrent: bool = True) -> list[str]:
    """The reviewed DDL that turns a plain ``history_row_change`` into a
    partitioned table, keeping existing rows as the first (legacy) partition.

    ``cutoff`` must be a month start: entries before it stay on the old table,
    everything later routes to monthly partitions (or DEFAULT until ``ensure``
    runs). Steps 1-6 run online ahead of the maintenance window; steps 7+ hold
    an ``ACCESS EXCLUSIVE`` lock and finish in one transaction. ``concurrent``
    builds the legacy primary-key index without locking writers (requires
    running outside a transaction, i.e. psql/AUTOCOMMIT).
    """
    bound = cutoff.strftime("%Y-%m-%d %H:%M:%S+00:00")
    legacy = f"{TABLE}_legacy"
    unique = (
        "UNIQUE INDEX CONCURRENTLY IF NOT EXISTS" if concurrent else "UNIQUE INDEX IF NOT EXISTS"
    )
    return [
        # -- online preparation (no exclusive lock) ---------------------------
        f"ALTER TABLE {TABLE} DROP CONSTRAINT IF EXISTS pk_{TABLE};",
        f"CREATE {unique} pk_{legacy} ON {TABLE} (id, occurred_at);",
        f"ALTER TABLE {TABLE} ADD CONSTRAINT pk_{legacy} PRIMARY KEY USING INDEX pk_{legacy};",
        (
            f"ALTER TABLE {TABLE} ADD CONSTRAINT ck_{legacy}_bounds "
            f"CHECK (occurred_at < TIMESTAMPTZ '{bound}') NOT VALID;"
        ),
        f"ALTER TABLE {TABLE} VALIDATE CONSTRAINT ck_{legacy}_bounds;",
        *(
            f"ALTER INDEX IF EXISTS {name} RENAME TO {name.replace(TABLE, legacy, 1)};"
            for name in _LEGACY_INDEXES
        ),
        f"DROP TRIGGER IF EXISTS {TABLE}_immutable ON {TABLE};",
        f"DROP TRIGGER IF EXISTS {TABLE}_no_truncate ON {TABLE};",
        # -- maintenance window (one transaction) ------------------------------
        "BEGIN;",
        f"LOCK TABLE {TABLE} IN ACCESS EXCLUSIVE MODE;",
        f"ALTER TABLE {TABLE} RENAME TO {legacy};",
        f"CREATE TABLE {TABLE} ({_COLUMNS}) PARTITION BY RANGE (occurred_at);",
        f"ALTER TABLE {TABLE} ADD CONSTRAINT pk_{TABLE} PRIMARY KEY (id, occurred_at);",
        (
            f"ALTER TABLE {TABLE} ADD CONSTRAINT ck_{TABLE}_op "
            "CHECK (op IN ('create', 'update', 'delete'));"
        ),
        (
            f"ALTER TABLE {TABLE} ADD CONSTRAINT ck_{TABLE}_source "
            "CHECK (source IN ('admin_ui', 'cli', 'job', 'api', 'system', 'revert', 'db_trigger'));"
        ),
        *(
            f"CREATE INDEX {name} ON {TABLE} ({columns});"
            for name, columns in (
                ("ix_history_row_change_model_object", "model_label, object_pk, id"),
                ("ix_history_row_change_occurred_at", "occurred_at"),
                ("ix_history_row_change_model_label", "model_label"),
                ("ix_history_row_change_change_set_id", "change_set_id"),
            )
        ),
        (
            f"ALTER TABLE {TABLE} ATTACH PARTITION {legacy} "
            f"FOR VALUES FROM (MINVALUE) TO (TIMESTAMPTZ '{bound}');"
        ),
        # The serial sequence is shared by the legacy partition: free it so
        # dropping that partition later never drops the sequence.
        "ALTER SEQUENCE IF EXISTS history_row_change_id_seq OWNED BY NONE;",
        f"CREATE TABLE IF NOT EXISTS {DEFAULT_PARTITION} PARTITION OF {TABLE} DEFAULT;",
        (
            "CREATE OR REPLACE FUNCTION history_forbid_mutation() RETURNS trigger AS $$ "
            "BEGIN IF current_setting('obox.history_maintenance', true) = 'on' THEN "
            "RETURN COALESCE(NEW, OLD); END IF; "
            f"RAISE EXCEPTION '{TABLE} is append-only (%)', TG_OP USING ERRCODE = '42501'; "
            "END $$ LANGUAGE plpgsql;"
        ),
        (
            f"CREATE TRIGGER {TABLE}_immutable BEFORE UPDATE OR DELETE ON {TABLE} "
            "FOR EACH ROW EXECUTE FUNCTION history_forbid_mutation();"
        ),
        (
            f"CREATE TRIGGER {TABLE}_no_truncate BEFORE TRUNCATE ON {TABLE} "
            "FOR EACH STATEMENT EXECUTE FUNCTION history_forbid_mutation();"
        ),
        "COMMIT;",
    ]


def next_month_cutoff(now: datetime | None = None) -> datetime:
    return add_months(month_start(now or datetime.now(UTC)), 1)
