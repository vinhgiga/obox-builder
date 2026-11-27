from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from fastapi import Request
from fastapi.responses import Response

from obox.core.async_utils import invoke
from obox.core.exceptions import ImproperlyConfigured, NotFound
from obox.db.session import session_scope
from obox.views.generic._model import (
    ModelContextMixin,
    ModelT,
    ModelTemplateMixin,
    field_options,
    reject_options,
)
from obox.views.generic.base import View


class SingleObjectMixin(ModelContextMixin[ModelT]):
    kwargs: dict[str, Any]
    request: Request
    object: ModelT | None = None

    def check(self) -> None:
        super().check()
        reject_options(
            self,
            (
                "queryset",
                "context_object_name",
                "pk_url_kwarg",
                "lookup_url_kwarg",
                "slug_url_kwarg",
                "slug_field",
            ),
        )
        if (
            getattr(self, "action", "retrieve") != "create"
            and self.model is None
            and self.resource is None
            and type(self).get_queryset is SingleObjectMixin.get_queryset
            and type(self).get_object is SingleObjectMixin.get_object
        ):
            raise ImproperlyConfigured("Define model, resource or get_queryset().")

    def get_queryset(self) -> sa.Select[tuple[ModelT]]:
        if resource := self.bound_resource():
            return resource.get_queryset()
        if self.model is not None:
            return sa.select(self.model)
        raise ImproperlyConfigured("Define model, resource or get_queryset().")

    async def get_object(self, queryset: sa.Select[tuple[ModelT]] | None = None) -> ModelT:
        query = await invoke(self.get_queryset) if queryset is None else queryset
        model = query.column_descriptions[0]["entity"]
        if "pk" not in self.kwargs:
            raise NotFound("Record is unavailable.")
        resource = self.model_resource(model)
        async with session_scope() as session:
            return await resource.bind(session, self.request.user).get_object(
                self.kwargs["pk"],
                queryset=query,
                action=getattr(self, "action", "retrieve"),
                for_update=self.request.method not in {"GET", "HEAD", "OPTIONS"},
            )

    async def restore_object(self) -> None:
        if self.object is not None:
            async with session_scope() as session:
                resource = self.model_resource(type(self.object))
                self.object = await resource.bind(session, self.request.user).reload(self.object)

    def get_context_data(self, **kwargs: Any) -> Any:
        return super().get_context_data(object=self.object, **kwargs)


class SingleObjectTemplateResponseMixin(ModelTemplateMixin, SingleObjectMixin[ModelT]):
    template_name_suffix = "_detail"

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
        return self.model_template_names(model, self.template_name_suffix.removeprefix("_"))


class DetailView(SingleObjectTemplateResponseMixin[ModelT], View):
    fields: Sequence[str] | None = None

    def check(self) -> None:
        super().check()
        if self.fields is not None and (
            not isinstance(self.fields, Sequence)
            or isinstance(self.fields, str)
            or any(not isinstance(name, str) for name in self.fields)
        ):
            raise ImproperlyConfigured("DetailView.fields must be a sequence of field names.")

    async def get(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        self.object = obj = await self.get_object()
        resource = self.model_resource(type(obj))
        options = field_options(resource)
        names = resource.readable if self.fields is None else self.fields
        values = [
            {**options[name], "value": resource.specs[name].serialize(getattr(obj, name))}
            for name in dict.fromkeys(names)
            if name in options
        ]
        return self.render_to_response(
            await invoke(
                self.get_context_data,
                **{
                    **kwargs,
                    "title": resource.model.__name__,
                    "values": values,
                    "change_url": await self.write_url("update", resource, obj),
                    "delete_url": await self.write_url("delete", resource, obj),
                },
            )
        )
