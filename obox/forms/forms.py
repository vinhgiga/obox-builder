"""Schema-backed HTML forms with request-local controls and cached validation."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator, Mapping
from copy import deepcopy
from enum import Enum
from typing import Any, ClassVar, Generic, cast

from pydantic import BaseModel, TypeAdapter
from pydantic import ValidationError as SchemaValidationError
from typing_extensions import TypeVar

from obox.core.async_utils import invoke
from obox.core.exceptions import NON_FIELD_ERRORS, ImproperlyConfigured, ValidationError
from obox.forms._schema import MISSING, kind, schema_fields
from obox.forms.fields import (
    BooleanField,
    ChoiceField,
    Field,
    FileField,
    JSONField,
)
from obox.forms.widgets import FileInput

S = TypeVar("S", bound=BaseModel, default=BaseModel)


class BoundField:
    def __init__(self, form: Form[Any], field: Field, name: str) -> None:
        self.form, self.field, self.name = form, field, name
        self.label = field.label or name.replace("_", " ").capitalize()
        self.html_name = form.add_prefix(name)
        self.id_for_label = f"id_{self.html_name}"

    @property
    def errors(self) -> list[str]:
        return self.form.errors.get(self.name, [])

    def value(self) -> Any:
        value = (
            self.form._initial_value(self.name, self.field)
            if not self.form.is_bound or self.field.disabled
            else self.form._data_value(self.name, self.field)
        )
        if value is MISSING:
            return None
        prepare = getattr(self.field, "prepare_value", None)
        return prepare(value) if prepare is not None else value

    def __html__(self) -> str:
        attrs: dict[str, Any] = {"id": self.id_for_label}
        if self.field.required and self.form.use_required_attribute:
            attrs["required"] = True
        if self.field.disabled:
            attrs["disabled"] = True
        if isinstance(self.field, ChoiceField):
            self.field.widget.choices = self.field.choices
        value = self.value()
        if isinstance(self.field, JSONField) and (
            not self.form.is_bound or self.field.disabled or not isinstance(value, str)
        ):
            value = json.dumps(value, ensure_ascii=False)
        if isinstance(value, Enum):
            value = value.value
        return str(self.field.widget.render(self.html_name, value, attrs))

    def __str__(self) -> str:
        return self.__html__()

    @property
    def is_hidden(self) -> bool:
        return bool(self.field.widget.input_type == "hidden")


class Form(Generic[S]):
    schema: type[BaseModel] | None = None
    field_options: ClassVar[Mapping[str, Field]] = {}
    widgets: ClassVar[Mapping[str, Any]] = {}
    labels: ClassVar[Mapping[str, str]] = {}
    help_texts: ClassVar[Mapping[str, str]] = {}

    def __init__(
        self,
        data: Mapping[str, Any] | None = None,
        files: Mapping[str, Any] | None = None,
        *,
        schema: type[S] | None = None,
        initial: Mapping[str, Any] | None = None,
        prefix: str | None = None,
        empty_permitted: bool = False,
    ) -> None:
        chosen = schema or self.schema
        if not isinstance(chosen, type) or not issubclass(chosen, BaseModel):
            raise ImproperlyConfigured("Define a Pydantic form schema.")
        self._schema = cast(type[S], chosen)
        controls = schema_fields(self._schema)
        for base in reversed(type(self).__mro__):
            controls.update(vars(base).get("field_options", {}))
        self.fields = deepcopy(controls)
        for name, widget in self.widgets.items():
            if name not in self.fields:
                raise ImproperlyConfigured(f"Unknown form field: {name!r}.")
            self.fields[name].widget = widget() if isinstance(widget, type) else deepcopy(widget)
        for name, field in self.fields.items():
            field.label = self.labels.get(name, field.label)
            field.help_text = self.help_texts.get(name, field.help_text)
        self.data, self.files = data if data is not None else {}, files or {}
        self.is_bound = data is not None or files is not None
        self.initial, self.prefix = dict(initial or {}), prefix
        self.empty_permitted, self.use_required_attribute = empty_permitted, True
        self.cleaned_data: S | None = None
        self.validated_data: dict[str, Any] = {}
        self.errors: dict[str, list[str]] = {}
        self._error_codes: dict[str, list[str | None]] = {}
        self.error_details: list[dict[str, Any]] = []
        self._validated = False
        self._validation_lock = asyncio.Lock()
        self._defaults: dict[str, Any] = {}

    def _initial_value(self, name: str, field: Field) -> Any:
        if name in self.initial:
            return self.initial[name]
        if field.initial is not None:
            if callable(field.initial):
                if name not in self._defaults:
                    self._defaults[name] = field.initial()
                return self._defaults[name]
            return field.initial
        info = self._schema.model_fields.get(name)
        if info is not None and not info.is_required():
            if name not in self._defaults:
                self._defaults[name] = info.get_default(call_default_factory=True)
            return self._defaults[name]
        return field.initial

    def add_prefix(self, field_name: str) -> str:
        return f"{self.prefix}-{field_name}" if self.prefix else field_name

    def __getitem__(self, name: str) -> BoundField:
        return BoundField(self, self.fields[name], name)

    def __iter__(self) -> Iterator[BoundField]:
        return (self[name] for name in self.fields)

    def _data_value(self, name: str, field: Field) -> Any:
        key = self.add_prefix(name)
        if isinstance(field.widget, FileInput):
            return self.files.get(key, self.data.get(key))
        getlist = getattr(self.data, "getlist", None)
        if getattr(field.widget, "allow_multiple_selected", False) and getlist is not None:
            return getlist(key)
        return self.data.get(key, MISSING)

    def value(self, name: str, default: Any = "") -> Any:
        value = self[name].value()
        return default if value is None else value

    def errors_for(self, name: str) -> list[str]:
        return self.errors.get(name, [])

    def is_multipart(self) -> bool:
        return any(
            getattr(field.widget, "needs_multipart_form", False) for field in self.fields.values()
        )

    async def prepare(self) -> None:
        for field in self.fields.values():
            prepare = getattr(field, "prepare", None)
            if prepare is not None:
                await invoke(prepare)

    @property
    def changed_data(self) -> list[str]:
        changed = []
        for name, field in self.fields.items():
            if field.disabled:
                continue
            initial, value = self._initial_value(name, field), self._data_value(name, field)
            if value is MISSING:
                value = None
            has_changed = getattr(field, "has_changed", None)
            if has_changed is not None:
                different = has_changed(initial, value)
            else:
                prepare = getattr(field, "prepare_value", None)
                if prepare:
                    initial, value = prepare(initial), prepare(value)
                try:
                    info = self._schema.model_fields.get(name)
                    if initial in field.empty_values and value in field.empty_values:
                        different = False
                        continue
                    if info is not None:
                        adapter: TypeAdapter[Any] = TypeAdapter(cast(Any, info.annotation))
                        initial = adapter.validate_python(initial)
                        value = adapter.validate_python(value)
                    different = initial != value
                except (ValidationError, SchemaValidationError, TypeError, ValueError):
                    different = True
            if different:
                changed.append(name)
        return changed

    def has_changed(self) -> bool:
        return bool(self.changed_data)

    async def is_valid(self) -> bool:
        async with self._validation_lock:
            if not self._validated:
                await self.full_clean()
        return self.is_bound and not self.errors

    async def full_clean(self) -> None:
        self.cleaned_data, self.validated_data = None, {}
        self.errors, self._error_codes, self.error_details = {}, {}, []
        self._validated = False
        if not self.is_bound or (self.empty_permitted and not self.has_changed()):
            self._validated = True
            return
        data, resolved = {}, {}
        for name, field in self.fields.items():
            value = (
                self._initial_value(name, field)
                if field.disabled
                else self._data_value(name, field)
            )
            info = self._schema.model_fields.get(name)
            try:
                if info is None or hasattr(field, "queryset") or isinstance(field, FileField):
                    value = await field.clean(None if value is MISSING else value)
                    resolved[name] = value
                    if info is None:
                        self.validated_data[name] = value
                        continue
                    prepare = getattr(field, "prepare_value", None)
                    if prepare is not None:
                        value = prepare(value)
                if value is MISSING:
                    if isinstance(field, BooleanField):
                        value = False
                    elif not info.is_required():
                        value = self._initial_value(name, field)
                    else:
                        continue
                if value == "" and kind(info.annotation)[1]:
                    value = None
                if isinstance(field, JSONField) and isinstance(value, str):
                    value = json.loads(value)
                data[name] = value
            except (ValidationError, ValueError) as exc:
                self.add_error(name, exc)
        data.update(
            {
                name: value
                for name, value in self.initial.items()
                if name in self._schema.model_fields and name not in self.fields
            }
        )
        try:
            data = self.prepare_data(data, resolved)
            parsed = self._schema.model_validate(data, by_name=True, by_alias=False)
            if not self.errors:
                self.cleaned_data = parsed
                self.validated_data.update(
                    {name: getattr(parsed, name) for name in self._schema.model_fields}
                )
                self.validated_data.update(resolved)
        except SchemaValidationError as exc:
            self.error_details = [
                dict(error)
                for error in exc.errors(
                    include_input=False, include_context=False, include_url=False
                )
            ]
            for error in self.error_details:
                error_field = str(error["loc"][0]) if error["loc"] else None
                if error_field is not None and error_field in self.errors:
                    continue
                code, message = error["type"], error["msg"]
                self.add_error(
                    error_field if error_field in self.fields else None,
                    ValidationError(message, code=code),
                )
        if self.cleaned_data is not None:
            await self.post_clean()
        self._validated = True

    def prepare_data(self, data: dict[str, Any], resolved: dict[str, Any]) -> dict[str, Any]:
        return data

    async def post_clean(self) -> None:
        pass

    def add_error(self, field: str | None, error: Any) -> None:
        error = error if isinstance(error, ValidationError) else ValidationError(error)
        errors = (
            error.message_dict
            if hasattr(error, "message_dict")
            else {field or NON_FIELD_ERRORS: error.messages}
        )
        for name, messages in errors.items():
            if name != NON_FIELD_ERRORS and name not in self.fields:
                raise ValueError(f"{type(self).__name__!r} has no field named {name!r}.")
            self.errors.setdefault(name, []).extend(messages)
            self._error_codes.setdefault(name, []).append(error.code)
            self.validated_data.pop(name, None)
        self.cleaned_data = None

    def non_field_errors(self) -> list[str]:
        return self.errors.get(NON_FIELD_ERRORS, [])

    def has_error(self, field: str, code: str | None = None) -> bool:
        return field in self.errors and (code is None or code in self._error_codes.get(field, ()))
