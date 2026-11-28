from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from fastapi import Request
from fastapi.responses import Response

from obox.core.async_utils import invoke
from obox.core.exceptions import ImproperlyConfigured, NotFound
from obox.views.generic._model import (
    ModelContextMixin,
    ModelT,
    ModelTemplateMixin,
    field_options,
)
from obox.views.generic.base import View


class SingleObjectMixin(ModelContextMixin[ModelT]):
    kwargs: dict[str, Any]
    request: Request
    object: ModelT | None = None

    async def get_object(self) -> ModelT:
        if "pk" not in self.kwargs:
            raise NotFound("Record is unavailable.")
        return await self.bound_resource().get_object(
            self.kwargs["pk"],
            action=getattr(self, "action", "retrieve"),
        )

    async def restore_object(self) -> None:
        if self.object is not None:
            self.object = await self.bound_resource().reload(self.object)

    def get_context_data(self, **kwargs: Any) -> Any:
        return super().get_context_data(object=self.object, **kwargs)


class SingleObjectTemplateResponseMixin(ModelTemplateMixin, SingleObjectMixin[ModelT]):
    template_name_suffix = "_detail"

    def get_template_names(self) -> list[str]:
        if self.template_name:
            return [self.template_name]
        resource = self.bound_resource()
        model = resource.model
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
        resource = self.bound_resource()
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
