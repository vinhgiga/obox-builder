from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import sqlalchemy as sa

from obox.db import _session
from obox.rest_framework.exceptions import NotFound
from obox.rest_framework.response import Response
from obox.rest_framework.settings import api_settings


class PageNumberPagination:
    page_size: int | None = None
    page_query_param = "page"
    page_size_query_param: str | None = None
    max_page_size: int | None = None
    last_page_strings = ("last",)

    async def paginate_queryset(self, queryset: Any, request: Any, view: Any = None) -> Any:
        size = self.page_size or api_settings.PAGE_SIZE
        if self.page_size_query_param and self.page_size_query_param in request.query_params:
            try:
                requested = int(request.query_params[self.page_size_query_param])
                if requested > 0:
                    size = min(requested, self.max_page_size) if self.max_page_size else requested
            except ValueError:
                pass
        if not size:
            return None
        async with _session() as session:
            self.count = int(
                await session.scalar(
                    sa.select(sa.func.count()).select_from(queryset.order_by(None).subquery())
                )
                or 0
            )
            self.num_pages = max(1, (self.count + size - 1) // size)
            raw = request.query_params.get(self.page_query_param, "1")
            try:
                self.number = self.num_pages if raw in self.last_page_strings else int(raw)
            except ValueError as exc:
                raise NotFound("Invalid page.") from exc
            if self.number < 1 or self.number > self.num_pages:
                raise NotFound("Invalid page.")
            result = list(
                (
                    await session.scalars(queryset.offset((self.number - 1) * size).limit(size))
                ).unique()
            )
        self.request = request
        return result

    def _link(self, number: int) -> str:
        url = urlsplit(str(self.request.url))
        params = [
            (key, value) for key, value in parse_qsl(url.query) if key != self.page_query_param
        ]
        if number != 1:
            params.append((self.page_query_param, str(number)))
        return urlunsplit(url._replace(query=urlencode(params)))

    def get_paginated_response(self, data: Any) -> Response:
        return Response(
            {
                "count": self.count,
                "next": self._link(self.number + 1) if self.number < self.num_pages else None,
                "previous": self._link(self.number - 1) if self.number > 1 else None,
                "results": data,
            }
        )
