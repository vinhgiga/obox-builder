"""Async pagination for model-admin list queries."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa

from obox_builder.contrib.admin.context import AdminContext


@dataclass(frozen=True)
class AdminPage:
    """One result page, with an optional exact total for large datasets."""

    objects: Sequence[Any]
    number: int
    per_page: int
    count: int | None
    num_pages: int | None
    has_next: bool
    has_previous: bool


class AdminPaginator:
    """Paginate a scoped SQLAlchemy select; subclasses can replace count behavior."""

    def __init__(
        self,
        ctx: AdminContext,
        queryset: sa.Select[Any],
        *,
        show_full_result_count: bool = True,
    ) -> None:
        self.ctx = ctx
        self.queryset = queryset
        self.show_full_result_count = show_full_result_count

    async def count(self) -> int:
        """Return the exact result count; override to use a cached or estimated count."""
        count_query = sa.select(sa.func.count()).select_from(
            self.queryset.order_by(None).subquery()
        )
        return int(await self.ctx.db.scalar(count_query) or 0)

    async def get_page(self, number: int, per_page: int) -> AdminPage:
        count = await self.count() if self.show_full_result_count else None
        offset = (number - 1) * per_page
        limit = per_page + 1 if count is None else per_page
        result = await self.ctx.db.execute(self.queryset.limit(limit).offset(offset))
        objects = result.scalars().unique().all()
        has_next = len(objects) > per_page if count is None else offset + len(objects) < count
        if count is None and has_next:
            objects = objects[:per_page]
        num_pages = max(1, -(-count // per_page)) if count is not None else None
        return AdminPage(
            objects=objects,
            number=number,
            per_page=per_page,
            count=count,
            num_pages=num_pages,
            has_next=has_next,
            has_previous=number > 1,
        )
