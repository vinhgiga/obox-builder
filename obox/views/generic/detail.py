from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from fastapi import Request
from fastapi.responses import Response
from starlette.exceptions import HTTPException

from obox.core.async_utils import invoke
from obox.core.exceptions import ImproperlyConfigured
from obox.db.identity import model_label
from obox.db.session import session_scope
from obox.resources import resolve_resource
from obox.views.generic._model import ModelContextMixin
from obox.views.generic.base import TemplateResponseMixin, View


class SingleObjectMixin(ModelContextMixin):
    queryset: Any = None
    context_object_name: str | None = None
    pk_url_kwarg = "pk"
    slug_url_kwarg = "slug"
    slug_field = "slug"
    kwargs: dict[str, Any]
    request: Request
    object: Any = None

    def check(self) -> None:
        super().check()
        if (
            getattr(self, "action", "retrieve") != "create"
            and self.model is None
            and self.resource is None
            and self.queryset is None
            and type(self).get_queryset is SingleObjectMixin.get_queryset
            and type(self).get_object is SingleObjectMixin.get_object
        ):
            raise ImproperlyConfigured("Define model, resource or queryset.")

    def get_queryset(self) -> Any:
        if resource := self.bound_resource():
            return resource.get_queryset()
        if self.queryset is not None:
            return self.queryset._generate()
        if self.model is not None:
            return sa.select(self.model)
        raise ImproperlyConfigured("Define model, queryset, or get_queryset().")

    async def get_object(self, queryset: Any = None) -> Any:
        query = await invoke(self.get_queryset) if queryset is None else queryset
        model = query.column_descriptions[0]["entity"]
        pk, slug = self.kwargs.get(self.pk_url_kwarg), self.kwargs.get(self.slug_url_kwarg)
        resource = self.bound_resource()
        if pk is not None and resource is not None:
            async with session_scope() as session:
                return await resource.bind(session, self.request.user).get_object(
                    pk,
                    queryset=query,
                    action=getattr(self, "action", "retrieve"),
                    for_update=self.request.method not in {"GET", "HEAD", "OPTIONS"},
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
        if resource is not None:
            query = resource.load_fields(query)
        async with session_scope() as session:
            obj = (await session.scalars(query)).unique().one_or_none()
        if obj is None:
            raise HTTPException(404, "Not found.")
        return obj

    async def restore_object(self) -> None:
        if self.object is not None:
            async with session_scope() as session:
                resource = self.bound_resource() or resolve_resource(type(self.object))
                self.object = await resource.bind(session, self.request.user).reload(self.object)

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
    bound_resource: Any

    def get_template_names(self) -> list[str]:
        if self.template_name:
            return [self.template_name]
        resource = self.bound_resource()
        model = (
            type(self.object)
            if self.object is not None
            else (resource.model if resource is not None else self.model)
        )
        if model is None:
            raise ImproperlyConfigured("Define a template or model.")
        app, name = model_label(model)
        return [
            f"{app}/{name}{self.template_name_suffix}.html",
            f"obox/generic/{self.template_name_suffix.removeprefix('_')}.html",
        ]


class DetailView(SingleObjectTemplateResponseMixin, SingleObjectMixin, View):
    change_url: str | None = None

    def get_context_data(self, **kwargs: Any) -> Any:
        context = super().get_context_data(**kwargs)
        model = type(self.object)
        resource = self.bound_resource()
        return {
            "title": model.__name__,
            "values": (
                [
                    (name, resource.specs[name].serialize(getattr(self.object, name)))
                    for name in resource.readable
                ]
                if resource is not None
                else []
            ),
            "change_url": self.change_url,
            **context,
        }

    async def get(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        self.object = await self.get_object()
        if (
            (resource := self.bound_resource())
            and "update" in self.route_names
            and self.has_model_permission(resource.model, "change")
            and await invoke(resource.can, "update", self.object, request.user)
        ):
            self.change_url = self.route_url(
                "update", pk=str(getattr(self.object, resource.pk_name))
            )
        return self.render_to_response(await invoke(self.get_context_data, **kwargs))
