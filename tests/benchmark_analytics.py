"""Run against an isolated PostgreSQL schema: uv run python tests/benchmark_analytics.py."""

from __future__ import annotations

import asyncio
import json
import os
import platform
import statistics
import tempfile
from decimal import Decimal
from pathlib import Path
from time import perf_counter

import sqlalchemy as sa
from test_analytics import STAFF, Duplicate, Sale, application


async def main() -> None:
    if "postgresql" not in os.getenv("OBOX_TEST_DATABASE_URL", ""):
        raise SystemExit("Set OBOX_TEST_DATABASE_URL to a disposable PostgreSQL database.")
    with tempfile.TemporaryDirectory(prefix="obox-analytics-benchmark-") as directory:
        async with application(Path(directory)) as (app, client, _):
            async with app.uow(principal=STAFF) as ctx:
                await ctx.session.execute(sa.delete(Duplicate))
                await ctx.session.execute(sa.delete(Sale))
                for start in range(0, 100_000, 5000):
                    await ctx.session.execute(
                        sa.insert(Sale),
                        [
                            {
                                "id": i + 1,
                                "tenant_id": 1,
                                "distributor": f"D{i % 4}",
                                "region": f"R{i % 3}",
                                "amount": Decimal(i % 10_000) / 100,
                            }
                            for i in range(start, start + 5000)
                        ],
                    )
                expected = str(await ctx.session.scalar(sa.select(sa.func.sum(Sale.amount))))
                version = await ctx.session.scalar(sa.select(sa.func.version()))
            widgets = ("pivot", "bar", "region", "pie")
            widget_times: list[float] = []
            dashboard_times: list[float] = []

            async def viewer() -> None:
                started = perf_counter()
                semaphore = asyncio.Semaphore(2)

                async def widget(slug: str) -> None:
                    async with semaphore:
                        queried = perf_counter()
                        response = await client.get(f"/analytics/sales/{slug}/data")
                        widget_times.append(perf_counter() - queried)
                        if response.status_code != 200:
                            raise AssertionError(response.text)
                        assert response.json()["records"] == 100_000
                        assert response.json()["total"] == expected

                await asyncio.gather(*(widget(slug) for slug in widgets))
                dashboard_times.append(perf_counter() - started)

            for _ in range(5):
                await asyncio.gather(*(viewer() for _ in range(10)))
            widget_p95 = statistics.quantiles(widget_times, n=100)[94]
            dashboard_p95 = statistics.quantiles(dashboard_times, n=100)[94]
            print(
                json.dumps(
                    {
                        "machine": platform.platform(),
                        "postgres": version,
                        "records": 100_000,
                        "concurrent_viewers": 10,
                        "dashboard_samples": len(dashboard_times),
                        "widget_samples": len(widget_times),
                        "widget_p95_ms": round(widget_p95 * 1000, 1),
                        "dashboard_p95_ms": round(dashboard_p95 * 1000, 1),
                        "passed": widget_p95 < 1 and dashboard_p95 < 3,
                    },
                    indent=2,
                )
            )
            assert widget_p95 < 1
            assert dashboard_p95 < 3


if __name__ == "__main__":
    asyncio.run(main())
