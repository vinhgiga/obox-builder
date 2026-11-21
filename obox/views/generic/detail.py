from __future__ import annotations

from typing import Any

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


class SingleObjectMixin(ContextMixin):
    model: type[Any] | None = None
    resource: type[Resource[Any]] | Resource[Any] | None = None
    queryset: Any = None
    context_object_name: str | None = None
    pk_url_kwarg = "pk"
    slug_url_kwarg = "slug"
    slug_field = "slug"
    kwargs: dict[str, Any]
    request: Request
    object: Any

    def _bound_resource(self) -> Resource[Any] | None:
        if isinstance(self.resource, type):
            if self.model is not None:
                raise ImproperlyConfigured("Specify either model or resource.")
            self.resource = resolve_resource(self.resource)
        return self.resource

    def get_queryset(self) -> Any:
        if resource := self._bound_resource():
            return resource._query(self.request.user)
        if self.queryset is not None:
            return self.queryset._generate()
        if self.model is not None:
            return sa.select(self.model)
        raise ImproperlyConfigured("Define model, queryset, or get_queryset().")

    async def get_object(self, queryset: Any = None) -> Any:
        query = await invoke(self.get_queryset) if queryset is None else queryset
        model = query.column_descriptions[0]["entity"]
        pk, slug = self.kwargs.get(self.pk_url_kwarg), self.kwargs.get(self.slug_url_kwarg)
        if pk is not None and (resource := self._bound_resource()):
            async with _session() as session:
                return await resource._get(
                    session, query, pk, lock=self.request.method not in {"GET", "HEAD", "OPTIONS"}
                )
        if pk is not None:
            column = sa.inspect(model).primary_key[0]
            try:
                if not isinstance(pk, column.type.python_type):
                    pk = column.type.python_type(pk)
            except (TypeError, ValueError) as exc:
                raise HTTPException(404, "Not found.") from exc
            query = query.where(column == pk)
        elif slug is not None:
            query = query.where(getattr(model, self.slug_field) == slug)
        else:
            raise AttributeError("A generic detail view requires a pk or slug.")
        async with _session() as session:
            obj = (await session.scalars(query)).unique().one_or_none()
        if obj is None:
            raise HTTPException(404, "Not found.")
        return obj

    def get_context_object_name(self, obj: Any) -> str:
        return self.context_object_name or type(obj).__name__.lower()

    def get_context_data(self, **kwargs: Any) -> Any:
        if self.object is not None:
            kwargs.update(
                {"object": self.object, self.get_context_object_name(self.object): self.object}
            )
        return super().get_context_data(**kwargs)


class SingleObjectTemplateResponseMixin(TemplateResponseMixin):
    template_name_suffix = "_detail"
    model: type[Any] | None
    request: Request
    object: Any

    def get_template_names(self) -> list[str]:
        if self.template_name:
            return [self.template_name]
        model = type(self.object) if self.object is not None else self.model
        if model is None:
            raise ImproperlyConfigured("Define a template or model.")
        app, name = _model_label(model)
        return [f"{app}/{name}{self.template_name_suffix}.html"]


class DetailView(SingleObjectTemplateResponseMixin, SingleObjectMixin, View):
    async def get(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        self.object = await self.get_object()
        return self.render_to_response(await invoke(self.get_context_data, **kwargs))
