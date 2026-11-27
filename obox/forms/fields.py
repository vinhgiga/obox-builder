"""Field conversion and validation for Obox forms."""

from __future__ import annotations

import asyncio
from functools import cache
from os import SEEK_END
from typing import Any, BinaryIO

from pydantic import TypeAdapter
from starlette.datastructures import UploadFile

from obox.core.exceptions import ValidationError
from obox.forms.widgets import CheckboxInput, FileInput, Select, Textarea, TextInput


class Field:
    widget: Any = TextInput
    empty_values: tuple[Any, ...] = (None, "", [], (), {})

    def __init__(
        self,
        *,
        required: bool = True,
        label: str | None = None,
        initial: Any = None,
        help_text: str = "",
        widget: Any = None,
        disabled: bool = False,
    ) -> None:
        self.required, self.label, self.initial = required, label, initial
        self.help_text, self.disabled = help_text, disabled
        chosen = widget or type(self).widget
        self.widget = chosen() if isinstance(chosen, type) else chosen
        self.error_messages = {
            "required": "This field is required.",
            "invalid": "Enter a valid value.",
        }

    def to_python(self, value: Any) -> Any:
        return None if value in self.empty_values else value

    async def clean(self, value: Any) -> Any:
        value = self.to_python(value)
        if self.required and value in self.empty_values:
            raise ValidationError(self.error_messages["required"], code="required")
        return value


class FileField(Field):
    widget = FileInput

    def to_python(self, value: Any) -> UploadFile | None:
        if value in self.empty_values:
            return None
        if not isinstance(value, UploadFile):
            raise ValidationError(self.error_messages["invalid"], code="invalid")
        return value if value.filename else None

    async def clean(self, value: Any) -> Any:
        await self._validate_size(value, self.widget)
        return await super().clean(value)

    @staticmethod
    async def _validate_size(value: object, widget: object) -> None:
        if not isinstance(value, UploadFile) or not value.filename:
            return
        limit = widget.max_size if isinstance(widget, FileInput) else None
        if limit is None:
            return
        size = value.size
        if size is None:
            try:
                size = await asyncio.to_thread(FileField._measure_size, value.file)
            except (OSError, ValueError) as exc:
                raise ValidationError(
                    "Cannot determine the uploaded file size.", code="invalid"
                ) from exc
        if size > limit:
            raise ValidationError(
                f"Ensure this file is at most {limit} bytes (it has {size}).",
                code="max_size",
            )

    @staticmethod
    def _measure_size(file: BinaryIO) -> int:
        position = file.tell()
        try:
            return file.seek(0, SEEK_END)
        finally:
            file.seek(position)


class BooleanField(Field):
    widget = CheckboxInput

    def to_python(self, value: Any) -> bool:
        return value not in (None, "", False, "false", "False", "0")


class JSONField(Field):
    widget: Any = Textarea
    # Empty arrays and objects are legitimate JSON values, not missing input.
    empty_values = (None, "")


@cache
def type_adapter(python_type: type[Any]) -> TypeAdapter[Any]:
    return TypeAdapter(python_type)


class ChoiceField(Field):
    widget = Select

    def __init__(self, *, choices: Any = (), **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.choices = list(choices)
