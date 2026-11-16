import shlex
from typing import Any

import sqlalchemy as sa

from obox.rest_framework.settings import api_settings


class BaseFilterBackend:
    def filter_queryset(self, request: Any, queryset: Any, view: Any) -> Any:
        raise NotImplementedError


class SearchFilter(BaseFilterBackend):
    def get_search_fields(self, view: Any, request: Any) -> Any:
        return getattr(view, "search_fields", None)

    def filter_queryset(self, request: Any, queryset: Any, view: Any) -> Any:
        fields = self.get_search_fields(view, request)
        search = request.query_params.get(api_settings.SEARCH_PARAM, "")
        if not fields or not search:
            return queryset
        model = queryset.column_descriptions[0]["entity"]
        for term in shlex.split(search.replace(",", " ")):
            predicates = []
            for name in fields:
                prefix = name[:1]
                column = getattr(model, name[1:] if prefix in ("^", "=") else name)
                predicates.append(
                    column.istartswith(term, autoescape=True)
                    if prefix == "^"
                    else sa.func.lower(column) == term.lower()
                    if prefix == "="
                    else column.icontains(term, autoescape=True)
                )
            queryset = queryset.where(sa.or_(*predicates))
        return queryset


class OrderingFilter(BaseFilterBackend):
    def filter_queryset(self, request: Any, queryset: Any, view: Any) -> Any:
        model = queryset.column_descriptions[0]["entity"]
        allowed = getattr(view, "ordering_fields", None)
        if allowed is None:
            serializer = view.get_serializer()
            allowed = [name for name, field in serializer.fields.items() if not field.write_only]
        elif allowed == "__all__":
            allowed = list(sa.inspect(model).column_attrs.keys())
        ordering = request.query_params.get(api_settings.ORDERING_PARAM)
        names = (
            [name.strip() for name in ordering.split(",") if name.strip().lstrip("-") in allowed]
            if ordering
            else []
        )
        if not names:
            defaults = getattr(view, "ordering", None)
            names = [defaults] if isinstance(defaults, str) else list(defaults or [])
        if names:
            queryset = queryset.order_by(None).order_by(
                *(
                    getattr(model, name[1:]).desc()
                    if name.startswith("-")
                    else getattr(model, name)
                    for name in names
                )
            )
        return queryset
