from __future__ import annotations

from typing import Any

from obox._async import invoke
from obox.rest_framework.settings import api_settings
from obox.rest_framework.views import APIView


class GenericAPIView(APIView):
    queryset: Any = None
    serializer_class: Any = None
    filter_backends: Any = None
    pagination_class: Any = None
    lookup_field = "pk"
    lookup_url_kwarg: str | None = None

    def get_queryset(self) -> Any:
        if self.queryset is None:
            raise AssertionError("Define queryset or override get_queryset().")
        return self.queryset._generate()

    def get_serializer_class(self) -> Any:
        if self.serializer_class is None:
            raise AssertionError("Define serializer_class or get_serializer_class().")
        return self.serializer_class

    def get_serializer_context(self) -> dict[str, Any]:
        return {"request": self.request, "view": self, "format": None}

    def get_serializer(self, *args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("context", self.get_serializer_context())
        return self.get_serializer_class()(*args, **kwargs)

    async def filter_queryset(self, queryset: Any) -> Any:
        backends = (
            api_settings.DEFAULT_FILTER_BACKENDS
            if self.filter_backends is None
            else self.filter_backends
        )
        for backend in backends:
            queryset = await invoke(backend().filter_queryset, self.request, queryset, self)
        return queryset

    async def get_object(self, queryset: Any = None) -> Any:
        import sqlalchemy as sa

        from obox.db import _session
        from obox.rest_framework.exceptions import NotFound

        queryset = (
            await self.filter_queryset(await invoke(self.get_queryset))
            if queryset is None
            else queryset
        )
        model = queryset.column_descriptions[0]["entity"]
        column = (
            sa.inspect(model).primary_key[0]
            if self.lookup_field == "pk"
            else getattr(model, self.lookup_field)
        )
        key = self.kwargs[self.lookup_url_kwarg or self.lookup_field]
        try:
            value = (
                key if isinstance(key, column.type.python_type) else column.type.python_type(key)
            )
        except (TypeError, ValueError, AttributeError) as exc:
            raise NotFound() from exc
        async with _session() as session:
            obj = (await session.scalars(queryset.where(column == value))).unique().one_or_none()
        if obj is None:
            raise NotFound()
        await self.check_object_permissions(self.request, obj)
        return obj

    @property
    def paginator(self) -> Any:
        if not hasattr(self, "_paginator"):
            cls = self.pagination_class
            if cls is None and "pagination_class" not in type(self).__dict__:
                cls = api_settings.DEFAULT_PAGINATION_CLASS
            self._paginator = cls() if cls else None
        return self._paginator

    async def paginate_queryset(self, queryset: Any) -> Any:
        return (
            await self.paginator.paginate_queryset(queryset, self.request, view=self)
            if self.paginator
            else None
        )

    def get_paginated_response(self, data: Any) -> Any:
        return self.paginator.get_paginated_response(data)
