"""Download all visible records in the current list as a private CSV file."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterator, Sequence
from dataclasses import replace
from decimal import Decimal
from tempfile import SpooledTemporaryFile

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from obox_builder.contrib.admin.context import AdminContext
from obox_builder.contrib.admin.list_query import list_queryset, load_query
from obox_builder.contrib.admin.relations import build_cells
from obox_builder.fields import QuerySpecError

EXPORT_BATCH_SIZE = 500


def _safe_text(value: str) -> str:
    if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
        return "'" + value
    return value


async def export_csv(ctx: AdminContext) -> StreamingResponse:
    assert ctx.admin is not None  # noqa: S101
    admin = ctx.admin
    admin.check_permission(ctx, "view")
    try:
        query, _ = await load_query(ctx)
        stmt, _ = list_queryset(ctx, query, admin.get_query_specs(ctx))
    except QuerySpecError as exc:
        raise HTTPException(400, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    columns = [spec for spec in admin.get_list_specs(ctx) if not spec.secret]
    selection = ctx.request.query_params.get("columns")
    if selection is not None:
        try:
            names = json.loads(selection)
            by_name = {spec.name: spec for spec in columns}
            if (
                not isinstance(names, list)
                or not names
                or len(names) > len(columns)
                or any(not isinstance(name, str) or name not in by_name for name in names)
                or len(set(names)) != len(names)
            ):
                raise ValueError
            columns = [by_name[name] for name in names]
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, "Select valid columns to export.") from exc
    # The response iterator owns the file after this function returns.
    file = SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")  # noqa: SIM115
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)

    def write(values: Sequence[str | int | float | Decimal]) -> None:
        buffer.seek(0)
        buffer.truncate()
        writer.writerow(
            [_safe_text(value) if isinstance(value, str) else value for value in values]
        )
        file.write(buffer.getvalue().encode("utf-8"))

    try:
        file.write(b"\xef\xbb\xbf")
        write([spec.label for spec in columns])
        seen = set()
        offset = 0
        while True:
            result = await ctx.db.scalars(stmt.offset(offset).limit(EXPORT_BATCH_SIZE))
            objects = result.unique().all()
            if not objects:
                break
            visible = []
            for obj in objects:
                pk = admin.get_pk(obj)
                if pk not in seen and admin.has_view_permission(replace(ctx, obj=obj), obj):
                    visible.append(obj)
                seen.add(pk)
            for cells in await build_cells(ctx, admin, columns, visible):
                write(
                    [
                        cell.value
                        if cell.kind == "number" and isinstance(cell.value, (int, float, Decimal))
                        else cell.text
                        for cell in cells
                    ]
                )
            offset += EXPORT_BATCH_SIZE
        # Release the shared authentication transaction before a slow download.
        await ctx.db.commit()
        file.seek(0)
    except BaseException:
        file.close()
        raise

    def chunks() -> Iterator[bytes]:
        try:
            while chunk := file.read(64 * 1024):
                yield chunk
        finally:
            file.close()

    return StreamingResponse(
        chunks(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{admin.model_name}.csv"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
