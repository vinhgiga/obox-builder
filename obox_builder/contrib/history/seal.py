"""Tamper evidence: hash-chained seal blocks over the append-only entries.

The append-only trigger is an accident guardrail; a malicious writer with
enough privileges can still edit, delete or reorder entries. ``history_seal``
reads entries older than a *lag window* (wider than any uncommitted
transaction) in ``id`` order, hashes each row, builds a Merkle tree per block
and chains the block hash to the previous one. ``history_verify`` recomputes
everything from the current table and reports mismatches, missing ids and
broken chains. Block hashes are logged to ``obox.history.audit`` so an
operator can ship them to WORM storage / a log service for external anchoring.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from obox_builder.contrib.history.models import RowChange, SealBlock
from obox_builder.core.utils.time import ensure_aware, utcnow

logger = logging.getLogger("obox.history.audit")

MAX_BATCH_SIZE = 10_000
DEFAULT_LAG_SECONDS = 600
MISSING_SAMPLE = 100


@dataclass
class SealResult:
    """What one ``history_seal`` run did (or would do, with ``dry_run``)."""

    sealed: int = 0
    blocks: int = 0
    dry_run: bool = False
    dry_run_ids: tuple[int, int] | None = None


@dataclass
class VerifyResult:
    """What ``history_verify`` found while recomputing every seal."""

    blocks: int = 0
    sealed_entries: int = 0
    missing_count: int = 0
    missing_ids: list[int] = field(default_factory=list)
    mismatched_blocks: list[int] = field(default_factory=list)
    broken_chain: list[int] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.missing_count or self.mismatched_blocks or self.broken_chain)


#: Explicit, stable column order for the canonical row hash.
_HASH_FIELDS = (
    "id",
    "change_set_id",
    "occurred_at",
    "op",
    "model_label",
    "object_pk",
    "object_repr",
    "changes",
    "schema_version",
    "row_version",
    "actor_id",
    "actor_label",
    "source",
    "request_id",
    "ip",
    "user_agent",
    "reason",
    "reverts_id",
)


def entry_hash(row: RowChange) -> str:
    """SHA-256 over the canonical JSON of one entry (all columns, fixed order)."""
    payload: dict[str, Any] = {}
    for name in _HASH_FIELDS:
        value = getattr(row, name)
        if name == "occurred_at":
            value = ensure_aware(value).isoformat()
        elif name == "change_set_id":
            value = str(value)
        if isinstance(value, dict):
            value = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        payload[name] = value
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def merkle_root(leaf_hashes: list[str]) -> str:
    """Binary Merkle tree over hex leaf hashes (odd node = duplicated)."""
    if not leaf_hashes:
        return hashlib.sha256(b"").hexdigest()
    level = [bytes.fromhex(leaf) for leaf in leaf_hashes]
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [
            hashlib.sha256(level[index] + level[index + 1]).digest()
            for index in range(0, len(level), 2)
        ]
    return level[0].hex()


def block_hash(
    *, prev_block_hash: str | None, first_id: int, last_id: int, count: int, root: str
) -> str:
    blob = f"{prev_block_hash or ''}|{first_id}|{last_id}|{count}|{root}"
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


async def _last_seal(db: AsyncSession) -> SealBlock | None:
    return await db.scalar(sa.select(SealBlock).order_by(SealBlock.block_id.desc()).limit(1))


async def seal(
    db: AsyncSession,
    *,
    lag_seconds: int = DEFAULT_LAG_SECONDS,
    batch_size: int = MAX_BATCH_SIZE,
    dry_run: bool = False,
) -> SealResult:
    """Seal unsealed entries older than the lag window, in ``id`` order."""
    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}.")
    if lag_seconds < 0:
        raise ValueError("lag_seconds must be >= 0.")
    result = SealResult(dry_run=dry_run)
    cutoff = utcnow() - timedelta(seconds=lag_seconds)
    previous = await _last_seal(db)
    after_id = previous.last_id if previous is not None else 0
    prev_hash = previous.block_hash if previous is not None else None

    first_unsealed: int | None = None
    last_sealed_id: int | None = None
    while True:
        rows = list(
            await db.scalars(
                sa.select(RowChange)
                .where(RowChange.id > after_id, RowChange.occurred_at < cutoff)
                .order_by(RowChange.id)
                .limit(batch_size)
            )
        )
        if not rows:
            break
        if dry_run:
            first_unsealed = first_unsealed or rows[0].id
            last_sealed_id = rows[-1].id
            result.sealed += len(rows)
            result.blocks += 1
            after_id = rows[-1].id
            if len(rows) < batch_size:
                break
            continue
        root = merkle_root([entry_hash(row) for row in rows])
        digest = block_hash(
            prev_block_hash=prev_hash,
            first_id=rows[0].id,
            last_id=rows[-1].id,
            count=len(rows),
            root=root,
        )
        db.add(
            SealBlock(
                first_id=rows[0].id,
                last_id=rows[-1].id,
                count=len(rows),
                merkle_root=root,
                prev_block_hash=prev_hash,
                block_hash=digest,
            )
        )
        await db.commit()
        logger.info(
            "history_seal: block [%d..%d] count=%d block_hash=%s",
            rows[0].id,
            rows[-1].id,
            len(rows),
            digest,
        )
        result.sealed += len(rows)
        result.blocks += 1
        prev_hash = digest
        after_id = rows[-1].id
        if len(rows) < batch_size:
            break
    if dry_run and first_unsealed is not None and last_sealed_id is not None:
        result.dry_run_ids = (first_unsealed, last_sealed_id)
    logger.info(
        "history_seal: %s %d entry(ies) in %d block(s).",
        "would seal" if dry_run else "sealed",
        result.sealed,
        result.blocks,
    )
    return result


async def verify(db: AsyncSession) -> VerifyResult:
    """Recompute every seal and report mismatches, missing ids and chain breaks."""
    result = VerifyResult()
    previous_hash: str | None = None
    blocks = list(await db.scalars(sa.select(SealBlock).order_by(SealBlock.block_id)))
    for block in blocks:
        result.blocks += 1
        result.sealed_entries += block.count
        recomputed = block_hash(
            prev_block_hash=block.prev_block_hash,
            first_id=block.first_id,
            last_id=block.last_id,
            count=block.count,
            root=block.merkle_root,
        )
        if recomputed != block.block_hash or block.prev_block_hash != previous_hash:
            result.broken_chain.append(block.block_id)
        rows = list(
            await db.scalars(
                sa.select(RowChange)
                .where(RowChange.id >= block.first_id, RowChange.id <= block.last_id)
                .order_by(RowChange.id)
            )
        )
        expected = set(range(block.first_id, block.last_id + 1))
        present = {row.id for row in rows}
        for missing in sorted(expected - present):
            result.missing_count += 1
            if len(result.missing_ids) < MISSING_SAMPLE:
                result.missing_ids.append(missing)
        if (
            len(rows) != block.count
            or merkle_root([entry_hash(row) for row in rows]) != block.merkle_root
        ):
            result.mismatched_blocks.append(block.block_id)
        previous_hash = block.block_hash
    logger.info(
        "history_verify: %d block(s), %d missing, %d mismatched, %d broken chain(s).",
        result.blocks,
        result.missing_count,
        len(result.mismatched_blocks),
        len(result.broken_chain),
    )
    return result
