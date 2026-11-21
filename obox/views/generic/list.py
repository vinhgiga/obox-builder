from __future__ import annotations

from typing import Any, cast

import sqlalchemy as sa
from fastapi import Request
from fastapi.responses import Response
from starlette.exceptions import HTTPException

from obox._async import invoke
from obox.core.exceptions import ImproperlyConfigured
from obox.db import _session
from obox.db._identity import _model_label
from obox.resources import Resource, resolve_resource
from obox.views.generic.base import ContextMixin, TemplateResponseMixin, View


class _Page:
    def __init__(self, objects: Any, number: int, count: int, per_page: int) -> None:
        self.object_list, self.number = objects, number
        from types import SimpleNamespace

        self.paginator = SimpleNamespace(
            count=count, per_page=per_page, num_pages=max(1, (count + per_page - 1) // per_page)
        )

    def has_next(self) -> bool:
        return bool(self.number < self.paginator.num_pages)

    def has_previous(self) -> bool:
        return self.number > 1

    def next_page_number(self) -> int:
        if not self.has_next():
            raise ValueError("That page contains no results")
        return self.number + 1

    def previous_page_number(self) -> int:
        if not self.has_previous():
            raise ValueError("That page number is less than 1")
        return self.number - 1


class ListView(TemplateResponseMixin, ContextMixin, View):
    model: type[Any] | None = None
    resource: type[Resource[Any]] | Resource[Any] | None = None
    queryset: Any = None
    context_object_name: str | None = None
    paginate_by: int | None = None
    page_kwarg = "page"
    allow_empty = True
    ordering: Any = None
    template_name_suffix = "_list"
    object_list: Any

    def get_ordering(self) -> Any:
        return self.ordering

    def _bound_resource(self) -> Resource[Any] | None:
        if isinstance(self.resource, type):
            if self.model is not None:
                raise ImproperlyConfigured("Specify either model or resource.")
            self.resource = resolve_resource(self.resource)
        return self.resource

    def get_queryset(self) -> Any:
        if resource := self._bound_resource():
            return resource._query(self.request.user).order_by(*resource._order())
        if self.queryset is not None:
            query = self.queryset._generate()
        elif self.model is not None:
            query = sa.select(self.model)
        else:
            raise ImproperlyConfigured("Define model, queryset, or get_queryset().")
        ordering = self.get_ordering()
        if ordering:
            model = query.column_descriptions[0]["entity"]
            names = (ordering,) if isinstance(ordering, str) else ordering
            query = query.order_by(
                *(
                    getattr(model, name.lstrip("-")).desc()
                    if name.startswith("-")
                    else getattr(model, name)
                    for name in names
                )
            )
        return query

    def get_context_data(self, **kwargs: Any) -> Any:
        model = self.model or self._list_model
        context = {
            "object_list": self.object_list,
            self.context_object_name or model.__name__.lower() + "_list": self.object_list,
            "is_paginated": self._page is not None and self._page.paginator.num_pages > 1,
            "page_obj": self._page,
            "paginator": self._page.paginator if self._page else None,
        }
        return cast(dict[str, Any], super().get_context_data(**{**context, **kwargs}))

    def get_template_names(self) -> list[str]:
        if self.template_name:
            return [self.template_name]
        app, name = _model_label(self.model or self._list_model)
        return [f"{app}/{name}{self.template_name_suffix}.html"]

    async def get(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        query = await invoke(self.get_queryset)
        self._list_model = query.column_descriptions[0]["entity"]
        self._page = None
        count, number = 0, 1
        resource = self._bound_resource()
        offset = 0
        async with _session() as session:
            if self.paginate_by:
                count = (
                    await resource._count(session, query)
                    if resource
                    else int(
                        await session.scalar(
                            sa.select(sa.func.count()).select_from(query.order_by(None).subquery())
                        )
                        or 0
                    )
                )
                raw = kwargs.get(self.page_kwarg, request.query_params.get(self.page_kwarg, "1"))
                try:
                    number = (
                        max(1, (count + self.paginate_by - 1) // self.paginate_by)
                        if raw == "last"
                        else int(raw)
                    )
                except ValueError as exc:
                    raise HTTPException(404, "Invalid page.") from exc
                if number < 1 or number > max(
                    1, (count + self.paginate_by - 1) // self.paginate_by
                ):
                    raise HTTPException(404, "Invalid page.")
                offset = (number - 1) * self.paginate_by
            if resource:
                self.object_list = await resource._rows(
                    session, query, offset=offset, limit=self.paginate_by
                )
            else:
                if self.paginate_by:
                    query = query.offset(offset).limit(self.paginate_by)
                self.object_list = list((await session.scalars(query)).unique())
            if self.paginate_by:
                self._page = _Page(self.object_list, number, count, self.paginate_by)
        if not self.allow_empty and not self.object_list:
            raise HTTPException(404, "Empty list.")
        return self.render_to_response(await invoke(self.get_context_data, **kwargs))
