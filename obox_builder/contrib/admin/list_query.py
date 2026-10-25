"""List query state, private saved views, and counts for visible group headers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import sqlalchemy as sa

from obox_builder.contrib.admin.context import AdminContext
from obox_builder.contrib.admin.models import SavedView
from obox_builder.fields import FieldSpec, QuerySpec, QuerySpecError

QUERY_KEYS = frozenset({"q", "f", "filter", "sort", "group", "size", "page"})


def view_scope(ctx: AdminContext) -> sa.Select[tuple[SavedView]]:
    assert ctx.admin is not None  # noqa: S101
    if not ctx.admin.has_view_permission(ctx):
        from obox_builder.core.exceptions import PermissionDenied

        raise PermissionDenied("You do not have permission to view this model.")
    return sa.select(SavedView).where(
        SavedView.owner_id == ctx.user.id,
        SavedView.site_name == ctx.site.name,
        SavedView.model_label == ctx.admin.model_label,
    )


async def load_query(ctx: AdminContext) -> tuple[QuerySpec, SavedView | None]:
    assert ctx.admin is not None  # noqa: S101
    scoped_views = view_scope(ctx)
    params: dict[str, Any] = {}
    selected = None
    view_id = ctx.request.query_params.get("view")
    if view_id:
        try:
            pk = int(view_id)
        except ValueError as exc:
            raise QuerySpecError("Invalid saved view.") from exc
        selected = await ctx.db.scalar(scoped_views.where(SavedView.id == pk))
        if selected is None:
            raise LookupError("Saved view does not exist.")
        params.update(selected.config)
    for key in QUERY_KEYS:
        if key in ctx.request.query_params:
            params[key] = (
                ctx.request.query_params.getlist(key)
                if key in {"f", "sort", "group"}
                else ctx.request.query_params[key]
            )
    specs = ctx.admin.get_query_specs(ctx)
    query = QuerySpec.parse(
        params,
        specs,
        default_sort=ctx.admin.get_ordering(ctx),
        default_size=ctx.admin.get_list_per_page(ctx),
    )
    return query, selected


async def group_headers(
    ctx: AdminContext,
    stmt: sa.Select[Any],
    query: QuerySpec,
    specs: Mapping[str, FieldSpec],
    objects: Sequence[Any],
) -> list[list[dict[str, Any]]]:
    """Count only the groups visible on this page, over the complete scoped query."""
    if not query.group or not objects:
        return [[] for _ in objects]
    assert ctx.admin is not None  # noqa: S101
    admin = ctx.admin
    fields = [specs[term.field] for term in query.group]
    source_columns = [spec.column for spec in fields if spec.column is not None]
    values = [
        tuple(
            getattr(obj, column.key or spec.name)
            for spec, column in zip(fields, source_columns, strict=True)
        )
        for obj in objects
    ]
    scope = (
        stmt.order_by(None)
        .with_only_columns(
            admin.pk_column(),
            *(column.label(f"group_{index}") for index, column in enumerate(source_columns)),
            maintain_column_froms=True,
        )
        .distinct()
        .subquery()
    )
    columns = [scope.c[f"group_{index}"] for index in range(len(fields))]
    counts: list[dict[tuple[Any, ...], int]] = []
    for depth in range(1, len(fields) + 1):
        prefixes = {value[:depth] for value in values}
        predicates = [
            sa.and_(
                *(column == value for column, value in zip(columns[:depth], prefix, strict=True))
            )
            for prefix in prefixes
        ]
        grouped = (
            sa.select(*columns[:depth], sa.func.count())
            .where(sa.or_(*predicates))
            .group_by(*columns[:depth])
        )
        result = (await ctx.db.execute(grouped)).all()
        counts.append({tuple(row[:-1]): row[-1] for row in result})
    headers: list[list[dict[str, Any]]] = []
    previous: tuple[Any, ...] | None = None
    for obj, value in zip(objects, values, strict=True):
        current = []
        for index, spec in enumerate(fields):
            if previous is None or previous[: index + 1] != value[: index + 1]:
                current.append(
                    {
                        "level": index + 1,
                        "label": spec.label,
                        "value": "Empty"
                        if value[index] is None
                        else admin.build_cell(spec, obj).text,
                        "count": counts[index][value[: index + 1]],
                    }
                )
        headers.append(current)
        previous = value
    return headers
