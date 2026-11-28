from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from typing import Any, ClassVar, cast

import sqlalchemy as sa
from fastapi import Request
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel
from sqlalchemy.orm import Mapper

from obox.core.async_utils import invoke
from obox.core.exceptions import (
    BuilderValidationError,
    ImproperlyConfigured,
    PermissionDenied,
    ResourceConflict,
    RuleViolation,
)
from obox.forms import Form, JSONField
from obox.resources import WriteAction
from obox.views.generic._model import ModelT
from obox.views.generic.base import ContextMixin, TemplateResponseMixin, View
from obox.views.generic.detail import SingleObjectTemplateResponseMixin
from obox.web.http import form_input


class FormMixin(ContextMixin):
    initial: ClassVar[dict[str, Any]] = {}
    form_class: type[Form[Any]] | None = None
    schema: type[BaseModel] | None = None
    invalid_status_code = 422
    success_url: str | None = None
    prefix: str | None = None
    request: Request
    render_to_response: Any
    reverse: Any

    def check(self) -> None:
        super().check()
        if self.schema is not None and (
            not isinstance(self.schema, type) or not issubclass(self.schema, BaseModel)
        ):
            raise ImproperlyConfigured("Define a Pydantic form schema.")
        if self.form_class is None and self.schema is None:
            raise ImproperlyConfigured("Define form_class or schema.")
        if (
            not self.success_url
            and not getattr(self, "route_names", {}).get("list")
            and type(self).get_success_url is FormMixin.get_success_url
            and type(self).form_valid is FormMixin.form_valid
        ):
            raise ImproperlyConfigured("No URL to redirect to. Provide a success_url.")

    def get_initial(self) -> dict[str, Any]:
        return deepcopy(self.initial)

    async def get_form_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "initial": self.get_initial(),
            "prefix": self.prefix,
        }
        if self.request.method in {"POST", "PUT", "PATCH"}:
            kwargs.update(await form_input(self.request))
        if self.schema is not None:
            kwargs["schema"] = self.schema
        return kwargs

    async def get_form(self) -> Form[Any]:
        form = (self.form_class or Form)(**await invoke(self.get_form_kwargs))
        await form.prepare()
        return form

    def get_success_url(self) -> str:
        if not self.success_url:
            raise ImproperlyConfigured("No URL to redirect to. Provide a success_url.")
        if self.success_url.startswith(("/", "http://", "https://")):
            return self.success_url
        return str(self.reverse(self.success_url))

    async def form_valid(self, form: Form[Any]) -> Response:
        return RedirectResponse(await invoke(self.get_success_url), status_code=303)

    async def form_invalid(self, form: Form[Any]) -> Response:
        return cast(
            Response,
            self.render_to_response(
                await invoke(self.get_context_data, form=form), status_code=self.invalid_status_code
            ),
        )

    async def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        if "form" not in kwargs:
            kwargs["form"] = await self.get_form()
        return cast(dict[str, Any], await invoke(super().get_context_data, **kwargs))


class FormView(TemplateResponseMixin, FormMixin, View):
    async def get(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return self.render_to_response(await invoke(self.get_context_data))

    async def post(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        form = await self.get_form()
        return cast(
            Response,
            await invoke(self.form_valid if await form.is_valid() else self.form_invalid, form),
        )


class _RecordFormView(SingleObjectTemplateResponseMixin[ModelT], FormView):
    action: WriteAction
    form_class: type[Form[Any]] = Form
    fieldsets: Sequence[tuple[str | None, dict[str, Any]]] = ()
    template_name_suffix = "_form"

    def check(self) -> None:
        super().check()
        if (
            not self.success_url
            and not self.route_names.get("list")
            and type(self).get_success_url is _RecordFormView.get_success_url
        ):
            raise ImproperlyConfigured("Provide a success_url.")

    def get_success_url(self) -> str:
        if not self.success_url and (url := self.route_url("list")):
            return url
        return super().get_success_url()

    async def get_form(self) -> Form[Any]:
        resource = self.bound_resource()
        self.object = obj = None if self.action == "create" else await self.get_object()
        if obj is None:
            await resource.check_permission("create")
        kwargs = await self.get_form_kwargs()
        if self.action == "delete":
            kwargs["schema"] = _DeleteInput
            return Form(**kwargs)
        initial = {}
        for name in resource.input_schema.model_fields:
            spec = resource.specs[name]
            default = spec.column.default
            if obj is not None:
                initial[name] = spec.serialize(getattr(obj, name))
            elif default is not None and default.is_scalar:
                initial[name] = default.arg
        kwargs.update(schema=resource.input_schema, initial={**initial, **kwargs["initial"]})
        form = self.form_class(**kwargs)
        allowed = await resource.writable_fields(self.action, obj)
        relationships = cast("Mapper[ModelT]", sa.inspect(resource.model)).relationships
        for name, field in form.fields.items():
            if name not in allowed:
                field.disabled = True
            elif name in relationships:
                control = relationships[name].info.get("obox_form_control")
                if control is not None:
                    form.fields[name] = await control(
                        resource.model, name, obj, self.request.state.obox_context
                    )
            elif isinstance(resource.specs[name].column.type, sa.JSON) and not isinstance(
                field, JSONField
            ):
                form.fields[name] = JSONField(required=field.required, widget=field.widget)
        permitted = {
            form.add_prefix(name) for name, field in form.fields.items() if not field.disabled
        }
        if (set(form.data) | set(form.files)) - permitted - {"csrf_token", "csrfmiddlewaretoken"}:
            raise PermissionDenied("These fields cannot be written.")
        await form.prepare()
        return form

    async def post(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return cast(Response, await invoke(self.form_valid, await self.get_form()))

    async def form_valid(self, form: Form[Any]) -> Response:
        data = await form.input_data(partial=True)
        if form.errors:
            return await self.form_invalid(form)
        resource = self.bound_resource()
        try:
            if self.object is None:
                self.object = await resource.create(data)
            elif self.action == "delete":
                await resource.destroy(self.object)
            else:
                self.object = await resource.update(self.object, data)
        except (BuilderValidationError, RuleViolation, ResourceConflict) as exc:
            await self.restore_object()
            errors = (
                exc.errors if isinstance(exc, BuilderValidationError) else {"__all__": [str(exc)]}
            )
            for name, messages in errors.items():
                field = name.partition(".")[0]
                form.add_error(
                    field if field in form.fields and not form.fields[field].disabled else None,
                    messages,
                )
            return await self.form_invalid(form)
        return await super().form_valid(form)

    async def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        if "form" not in kwargs:
            kwargs["form"] = await self.get_form()
        context = await super().get_context_data(**kwargs)
        resource = self.bound_resource()
        form = context["form"]
        readonly = {
            name: getattr(self.object, name, "") if self.object is not None else ""
            for name in resource.readable
            if name not in form.fields or form.fields[name].disabled
        }
        return {
            "title": resource.model.__name__,
            "fieldsets": [
                {
                    "title": title,
                    "description": options.get("description"),
                    "fields": [name for name in options["fields"] if name in resource.readable],
                }
                for title, options in self.fieldsets
            ],
            "readonly_fields": readonly.keys(),
            "readonly_values": readonly,
            "delete_url": await self.write_url("delete", resource, self.object)
            if self.object is not None and self.action != "delete"
            else None,
            **context,
        }


class CreateView(_RecordFormView[ModelT]):
    action = "create"


class UpdateView(_RecordFormView[ModelT]):
    action = "update"


class _DeleteInput(BaseModel):
    pass


class DeleteView(_RecordFormView[ModelT]):
    action = "delete"
    template_name_suffix = "_confirm_delete"

    def check(self) -> None:
        super().check()
        if self.success_url and any(char in self.success_url for char in "{}"):
            raise ImproperlyConfigured("DeleteView.success_url must be static.")

    async def delete(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return await self.post(request, *args, **kwargs)
