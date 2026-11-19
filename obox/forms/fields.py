"""Field conversion and validation for Obox forms."""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from functools import cache
from os import SEEK_END
from typing import Any, BinaryIO
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import TypeAdapter
from pydantic import ValidationError as PydanticValidationError
from starlette.datastructures import UploadFile

from obox._async import invoke
from obox.conf import settings
from obox.core.exceptions import ValidationError
from obox.forms.widgets import CheckboxInput, FileInput, NumberInput, Select, Textarea, TextInput


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
        validators: Any = (),
        error_messages: Any = None,
    ) -> None:
        self.required, self.label, self.initial = required, label, initial
        self.help_text, self.disabled = help_text, disabled
        chosen = widget or type(self).widget
        self.widget = chosen() if isinstance(chosen, type) else chosen
        self.validators = list(validators)
        self.error_messages = {
            "required": "This field is required.",
            "invalid": "Enter a valid value.",
            **(error_messages or {}),
        }

    def to_python(self, value: Any) -> Any:
        return None if value in self.empty_values else value

    async def clean(self, value: Any) -> Any:
        value = self.to_python(value)
        if self.required and value in self.empty_values:
            raise ValidationError(self.error_messages["required"], code="required")
        if value not in self.empty_values:
            for validator in self.validators:
                await invoke(validator, value)
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


class CharField(Field):
    def __init__(
        self,
        *,
        max_length: int | None = None,
        min_length: int | None = None,
        strip: bool = True,
        empty_value: Any = "",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.max_length, self.min_length, self.strip, self.empty_value = (
            max_length,
            min_length,
            strip,
            empty_value,
        )

    def to_python(self, value: Any) -> Any:
        if value in self.empty_values:
            return self.empty_value
        value = str(value).strip() if self.strip else str(value)
        if self.max_length is not None and len(value) > self.max_length:
            raise ValidationError(
                f"Ensure this value has at most {self.max_length} characters "
                f"(it has {len(value)}).",
                code="max_length",
            )
        if self.min_length is not None and len(value) < self.min_length:
            raise ValidationError(
                f"Ensure this value has at least {self.min_length} characters "
                f"(it has {len(value)}).",
                code="min_length",
            )
        return value or self.empty_value


class IntegerField(Field):
    widget = NumberInput

    def __init__(self, *, min_value: Any = None, max_value: Any = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.min_value, self.max_value = min_value, max_value

    def to_python(self, value: Any) -> Any:
        if value in self.empty_values:
            return None
        try:
            parsed = Decimal(str(value))
            if not parsed.is_finite() or parsed != parsed.to_integral_value():
                raise ValueError
            result = int(parsed)
        except (ValueError, InvalidOperation) as exc:
            raise ValidationError("Enter a whole number.", code="invalid") from exc
        if self.min_value is not None and result < self.min_value:
            raise ValidationError(
                f"Ensure this value is greater than or equal to {self.min_value}.", code="min_value"
            )
        if self.max_value is not None and result > self.max_value:
            raise ValidationError(
                f"Ensure this value is less than or equal to {self.max_value}.", code="max_value"
            )
        return result


class DecimalField(Field):
    widget = NumberInput

    def __init__(
        self, *, max_digits: int | None = None, decimal_places: int | None = None, **kwargs: Any
    ) -> None:
        super().__init__(**kwargs)
        self.max_digits, self.decimal_places = max_digits, decimal_places

    def to_python(self, value: Any) -> Any:
        if value in self.empty_values:
            return None
        try:
            result = Decimal(str(value))
            if not result.is_finite():
                raise ValueError
        except (ValueError, InvalidOperation) as exc:
            raise ValidationError("Enter a number.", code="invalid") from exc
        _, digits, exponent = result.as_tuple()
        decimals = max(-int(exponent), 0)
        total = max(len(digits), decimals) + max(int(exponent), 0)
        if self.max_digits is not None and total > self.max_digits:
            raise ValidationError(
                f"Ensure that there are no more than {self.max_digits} digits in total.",
                code="max_digits",
            )
        if self.decimal_places is not None and decimals > self.decimal_places:
            raise ValidationError(
                f"Ensure that there are no more than {self.decimal_places} decimal places.",
                code="max_decimal_places",
            )
        if (
            self.max_digits is not None
            and self.decimal_places is not None
            and total - decimals > self.max_digits - self.decimal_places
        ):
            raise ValidationError(
                f"Ensure that there are no more than {self.max_digits - self.decimal_places} "
                "digits before the decimal point.",
                code="max_whole_digits",
            )
        return result


class BooleanField(Field):
    widget = CheckboxInput

    def to_python(self, value: Any) -> bool:
        return False if value in (None, "", False, "false", "False", "0") else bool(value)

    async def clean(self, value: Any) -> bool:
        result = self.to_python(value)
        if self.required and not result:
            raise ValidationError(self.error_messages["required"], code="required")
        for validator in self.validators:
            await invoke(validator, result)
        return result


class FloatField(Field):
    widget = NumberInput

    def to_python(self, value: Any) -> Any:
        if value in self.empty_values:
            return None
        import math

        try:
            result = float(value)
            if not math.isfinite(result):
                raise ValueError
            return result
        except (ValueError, TypeError) as exc:
            raise ValidationError("Enter a number.", code="invalid") from exc


class JSONField(Field):
    widget = Textarea

    def to_python(self, value: Any) -> Any:
        if value in self.empty_values:
            return None
        if not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except ValueError as exc:
            raise ValidationError("Enter a valid JSON.", code="invalid") from exc


@cache
def _type_adapter(python_type: type[Any]) -> TypeAdapter[Any]:
    return TypeAdapter(python_type)


class _TypedField(Field):
    python_type: Any
    invalid = "Enter a valid value."

    def to_python(self, value: Any) -> Any:
        if value in self.empty_values:
            return None
        try:
            return _type_adapter(self.python_type).validate_python(value)
        except PydanticValidationError as exc:
            raise ValidationError(self.invalid, code="invalid") from exc


class DateField(_TypedField):
    python_type = date
    invalid = "Enter a valid date."


class DateTimeField(_TypedField):
    python_type = datetime
    invalid = "Enter a valid date/time."

    def to_python(self, value: Any) -> Any:
        result = super().to_python(value)
        if result is not None and result.tzinfo is None and settings.USE_TZ:
            result = result.replace(tzinfo=ZoneInfo(settings.TIME_ZONE))
        return result


class TimeField(_TypedField):
    python_type = time
    invalid = "Enter a valid time."


class UUIDField(_TypedField):
    python_type = UUID
    invalid = "Enter a valid UUID."


class ChoiceField(Field):
    widget = Select

    def __init__(self, *, choices: Any = (), **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.choices = list(choices)

    def to_python(self, value: Any) -> str:
        return "" if value in self.empty_values else str(value)

    async def clean(self, value: Any) -> Any:
        result = await super().clean(value)
        if result not in self.empty_values and result not in {str(key) for key, _ in self.choices}:
            raise ValidationError(
                f"Select a valid choice. {result} is not one of the available choices.",
                code="invalid_choice",
            )
        return result
