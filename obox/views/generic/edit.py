from __future__ import annotations

from typing import Any, ClassVar, cast

import sqlalchemy as sa
from fastapi import Request
from fastapi.responses import RedirectResponse, Response

from obox._async import invoke
from obox._exceptions import BuilderValidationError, PermissionDenied
from obox._http import form_input
from obox.core.exceptions import ImproperlyConfigured
from obox.db import _session
from obox.forms import Form, ModelForm, modelform_factory
from obox.resources import WriteAction
from obox.views.generic.base import ContextMixin, TemplateResponseMixin, View
from obox.views.generic.detail import SingleObjectMixin, SingleObjectTemplateResponseMixin


class FormMixin(ContextMixin):
    initial: ClassVar[dict[str, Any]] = {}
    form_class: Any = None
    success_url: str | None = None
    prefix: str | None = None
    request: Request
    render_to_response: Any

    def get_initial(self) -> dict[str, Any]:
        return self.initial.copy()

    def get_prefix(self) -> str | None:
        return self.prefix

    def get_form_class(self) -> Any:
        return self.form_class

    async def get_form_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"initial": self.get_initial(), "prefix": self.get_prefix()}
        if self.request.method in ("POST", "PUT"):
            kwargs.update(await form_input(self.request))
        return kwargs

    async def get_form(self, form_class: Any = None) -> Any:
        cls = form_class or await invoke(self.get_form_class)
        if cls is None:
            raise ImproperlyConfigured("Define form_class.")
        form = cls(**await invoke(self.get_form_kwargs))
        await form.prepare()
        return form

    def get_success_url(self) -> str:
        if not self.success_url:
            raise ImproperlyConfigured("No URL to redirect to. Provide a success_url.")
        return str(self.success_url)

    async def form_valid(self, form: Form) -> Response:
        return RedirectResponse(await invoke(self.get_success_url), status_code=303)

    async def form_invalid(self, form: Form) -> Response:
        return cast(
            Response, self.render_to_response(await invoke(self.get_context_data, form=form))
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
            (
                await invoke(self.form_valid, form)
                if await form.is_valid()
                else await invoke(self.form_invalid, form)
            ),
        )

    async def put(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return await self.post(request, *args, **kwargs)


class ModelFormMixin(FormMixin, SingleObjectMixin):
    fields: Any = None

    def get_form_class(self) -> Any:
        if self.fields is not None and self.form_class is not None:
            raise ImproperlyConfigured("Specifying both fields and form_class is not permitted.")
        if self.form_class is not None:
            return cast(type[ModelForm], self.form_class)
        if resource := self._bound_resource():
            return modelform_factory(resource.model, fields=self.fields or resource.writable)
        model = self.model or type(self.object)
        if self.fields is None:
            raise ImproperlyConfigured("Define fields or form_class.")
        return modelform_factory(model, fields=self.fields)

    async def get_form_kwargs(self) -> dict[str, Any]:
        return {**await super().get_form_kwargs(), "instance": self.object}

    async def get_form(self, form_class: Any = None) -> Any:
        form = await super().get_form(form_class)
        if resource := self._bound_resource():
            action: WriteAction = "create" if self.object is None else "update"
            await resource._authorize(action, self.object, self.request.user)
            allowed = await resource._writable(action, self.object, self.request.user)
            submitted = {
                name
                for name in resource.specs
                if form.add_prefix(name) in form.data or form.add_prefix(name) in form.files
            }
            if denied := submitted - allowed:
                raise PermissionDenied(f"Fields cannot be written: {', '.join(sorted(denied))}.")
            for name, field in form.fields.items():
                if name in resource.specs and name not in allowed:
                    field.disabled = True
        return form

    async def _save_resource_form(self, form: ModelForm) -> Any:
        resource = self._bound_resource()
        if resource is None:
            return await form.save()
        values, sources = form._column_data()
        for name, value in form.cleaned_data.items():
            if name in resource.specs and "obox_files" in resource.specs[name].column.info:
                values[name] = resource.specs[name].serialize(value)
                sources[name] = name
        values = {
            name: value
            for name, value in values.items()
            if not form.fields[sources[name]].disabled
            and not (
                name in resource.specs
                and (
                    (spec := resource.specs[name]).column.default is not None
                    or spec.column.server_default is not None
                )
                and form.add_prefix(sources[name]) not in form.data
                and not isinstance(spec.column.type, sa.Boolean)
            )
        }
        async with _session() as session:
            form.instance = await resource._save(session, self.request.user, values, self.object)
        return form.instance

    async def form_valid(self, form: Any) -> Response:
        try:
            self.object = await self._save_resource_form(form)
            if self._bound_resource():
                await form.save_m2m()
        except BuilderValidationError as exc:
            return await self._render_resource_errors(form, exc)
        return await super().form_valid(form)

    async def _render_resource_errors(
        self, form: ModelForm, exc: BuilderValidationError
    ) -> Response:
        async with _session() as session:
            if not session.is_active:
                raise exc
        for field, messages in exc.errors.items():
            for message in messages:
                form.add_error(field if field in form.fields else None, message)
        return await self.form_invalid(form)

    def get_success_url(self) -> str:
        if self.success_url:
            return self.success_url.format(**vars(self.object))
        if hasattr(self.object, "get_absolute_url"):
            return str(self.object.get_absolute_url())
        raise ImproperlyConfigured("No URL to redirect to. Provide a success_url.")


class CreateView(SingleObjectTemplateResponseMixin, ModelFormMixin, FormView):
    template_name_suffix = "_form"

    async def get(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        self.object = None
        return await super().get(request, *args, **kwargs)

    async def post(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        self.object = None
        return await super().post(request, *args, **kwargs)


class UpdateView(SingleObjectTemplateResponseMixin, ModelFormMixin, FormView):
    template_name_suffix = "_form"

    async def get(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        self.object = await self.get_object()
        return await super().get(request, *args, **kwargs)

    async def post(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        self.object = await self.get_object()
        return await super().post(request, *args, **kwargs)


class DeleteView(SingleObjectTemplateResponseMixin, SingleObjectMixin, FormMixin, View):
    template_name_suffix = "_confirm_delete"
    form_class = Form

    async def get(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        self.object = await self.get_object()
        if resource := self._bound_resource():
            await resource._authorize("delete", self.object, request.user)
        return self.render_to_response(await invoke(self.get_context_data, **kwargs))

    async def post(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        self.object = await self.get_object()
        form = await self.get_form()
        return cast(
            Response,
            await invoke(self.form_valid if await form.is_valid() else self.form_invalid, form),
        )

    def get_success_url(self) -> str:
        if not self.success_url:
            raise ImproperlyConfigured("No URL to redirect to. Provide a success_url.")
        return self.success_url.format(**vars(self.object))

    async def form_valid(self, form: Form) -> Response:
        url = self.get_success_url()
        async with _session() as session:
            if resource := self._bound_resource():
                await resource._delete(session, self.request.user, self.object)
            else:
                await session.delete(self.object)
                await session.flush()
        return RedirectResponse(url, status_code=303)

    async def delete(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        self.object = await self.get_object()
        return await self.form_valid(Form({}))
