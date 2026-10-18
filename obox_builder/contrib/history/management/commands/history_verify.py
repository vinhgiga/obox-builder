from __future__ import annotations

from obox_builder.core.management import utils
from obox_builder.core.management.base import CommandError, command


@command(help="Recompute the seal blocks and report tampering (nonzero exit).")
def handle() -> None:
    import obox_builder
    from obox_builder.contrib.history.seal import MISSING_SAMPLE, verify
    from obox_builder.db.engine import run_async
    from obox_builder.db.session import session_scope

    async def run() -> None:
        async with session_scope() as db:
            result = await verify(db)
        utils.echo(f"Checked {result.blocks} block(s), {result.sealed_entries} sealed entry(ies).")
        if result.missing_count:
            sample = ", ".join(str(part) for part in result.missing_ids)
            more = result.missing_count - len(result.missing_ids)
            utils.echo(
                f"Missing entries: {result.missing_count}"
                + (
                    f" (first {len(result.missing_ids)}: {sample}"
                    + (f", +{more} more)" if more else ")")
                )
            )
        if result.mismatched_blocks:
            utils.echo(
                "Mismatched blocks: " + ", ".join(str(part) for part in result.mismatched_blocks)
            )
        if result.broken_chain:
            utils.echo(
                "Broken chain at blocks: " + ", ".join(str(part) for part in result.broken_chain)
            )
        if not result.ok:
            raise CommandError(
                f"history_verify found tampering (sample ids capped at {MISSING_SAMPLE})."
            )
        utils.echo("OK: all seals verified.")

    obox_builder.setup()
    run_async(run())
