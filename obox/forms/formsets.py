"""Collections of forms with bounded input and shared validation."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from functools import cached_property
from typing import Any, ClassVar

from markupsafe import Markup
from pydantic import BaseModel
from pydantic import Field as SchemaField

from obox.core.async_utils import invoke
from obox.core.exceptions import ValidationError
from obox.forms.fields import BooleanField, IntegerField
from obox.forms.forms import Form
from obox.forms.widgets import CheckboxInput, HiddenInput, NumberInput

TOTAL_FORM_COUNT = "TOTAL_FORMS"
INITIAL_FORM_COUNT = "INITIAL_FORMS"
MIN_NUM_FORM_COUNT = "MIN_NUM_FORMS"
MAX_NUM_FORM_COUNT = "MAX_NUM_FORMS"
ORDERING_FIELD_NAME = "ORDER"
DELETION_FIELD_NAME = "DELETE"
DEFAULT_MAX_NUM = 1000


class ManagementInput(BaseModel):
    TOTAL_FORMS: int = SchemaField(ge=0)
    INITIAL_FORMS: int = SchemaField(ge=0)
    MIN_NUM_FORMS: int | None = None
    MAX_NUM_FORMS: int | None = None


class ManagementForm(Form[ManagementInput]):
    schema = ManagementInput
    widgets = dict.fromkeys(ManagementInput.model_fields, HiddenInput)


class BaseFormSet:
    form: type[Form[Any]] = Form
    extra = 1
    min_num = 0
    max_num = DEFAULT_MAX_NUM
    absolute_max = DEFAULT_MAX_NUM * 2
    validate_min = False
    validate_max = False
    can_delete = False
    can_delete_extra = True
    can_order = False
    deletion_widget = CheckboxInput
    ordering_widget = NumberInput
    default_error_messages: ClassVar[dict[str, str]] = {
        "missing_management_form": "ManagementForm data is missing or has been tampered with. "
        "Missing fields: "
        "%(field_names)s. You may need to file a bug report if the issue persists.",
        "too_many_forms": "Please submit at most %(num)d forms.",
        "too_few_forms": "Please submit at least %(num)d forms.",
    }

    def __init__(
        self,
        data: Any = None,
        files: Any = None,
        *,
        prefix: str | None = None,
        initial: Any = None,
        form_kwargs: Any = None,
        error_messages: Any = None,
    ) -> None:
        self.is_bound = data is not None or files is not None
        self.data, self.files = data if data is not None else {}, files or {}
        self.prefix = prefix if prefix is not None else self.get_default_prefix()
        self.initial, self.form_kwargs = initial or [], dict(form_kwargs or {})
        self.error_messages = {**self.default_error_messages, **(error_messages or {})}
        self.errors: list[dict[str, list[str]]] = []
        self._non_form_errors: list[str] = []
        self._validated = False
        self._validation_lock = asyncio.Lock()

    @classmethod
    def get_default_prefix(cls) -> str:
        return "form"

    def add_prefix(self, index: Any) -> str:
        return f"{self.prefix}-{index}"

    @cached_property
    def management_form(self) -> ManagementForm:
        return ManagementForm(
            self.data if self.is_bound else None,
            prefix=self.prefix,
            initial={
                TOTAL_FORM_COUNT: self.total_form_count(),
                INITIAL_FORM_COUNT: self.initial_form_count(),
                MIN_NUM_FORM_COUNT: self.min_num,
                MAX_NUM_FORM_COUNT: self.max_num,
            },
        )

    def _count(self, name: str) -> int:
        try:
            return max(0, IntegerField().to_python(self.data.get(self.add_prefix(name))) or 0)
        except ValidationError:
            return 0

    def initial_form_count(self) -> int:
        return self._count(INITIAL_FORM_COUNT) if self.is_bound else len(self.initial)

    def total_form_count(self) -> int:
        if self.is_bound:
            return min(self._count(TOTAL_FORM_COUNT), self.absolute_max)
        initial = self.initial_form_count()
        return max(initial, min(max(initial, self.min_num) + self.extra, self.max_num))

    def get_form_kwargs(self, index: int | None) -> dict[str, Any]:
        return self.form_kwargs.copy()

    def construct_form(self, index: int | None, **kwargs: Any) -> Form[Any]:
        options: dict[str, Any] = {
            **self.get_form_kwargs(index),
            "prefix": self.add_prefix("__prefix__" if index is None else index),
            "empty_permitted": index is None
            or (index >= self.initial_form_count() and index >= self.min_num),
        }
        if self.is_bound and index is not None:
            options.update(data=self.data, files=self.files)
        if index is not None and index < len(self.initial):
            options["initial"] = self.initial[index]
        form = self.form(**{**options, **kwargs})
        form.use_required_attribute = False
        self.add_fields(form, index)
        return form

    @cached_property
    def forms(self) -> list[Form[Any]]:
        return [self.construct_form(index) for index in range(self.total_form_count())]

    @property
    def initial_forms(self) -> list[Form[Any]]:
        return self.forms[: self.initial_form_count()]

    @property
    def extra_forms(self) -> list[Form[Any]]:
        return self.forms[self.initial_form_count() :]

    @property
    def empty_form(self) -> Form[Any]:
        return self.construct_form(None)

    def add_fields(self, form: Form[Any], index: int | None) -> None:
        if self.can_order:
            initial = index + 1 if index is not None and index < self.initial_form_count() else None
            form.fields[ORDERING_FIELD_NAME] = IntegerField(
                label="Order",
                required=False,
                initial=initial,
                widget=self.ordering_widget,
            )
        if self.can_delete and (
            self.can_delete_extra or (index is not None and index < self.initial_form_count())
        ):
            form.fields[DELETION_FIELD_NAME] = BooleanField(
                label="Delete",
                required=False,
                widget=self.deletion_widget,
            )

    def __iter__(self) -> Iterator[Form[Any]]:
        return iter(self.forms)

    def __getitem__(self, index: int) -> Form[Any]:
        return self.forms[index]

    def __len__(self) -> int:
        return len(self.forms)

    def __bool__(self) -> bool:
        return True

    def is_multipart(self) -> bool:
        form = self.forms[0] if self.forms else self.empty_form
        return form.is_multipart()

    async def prepare(self) -> None:
        for form in self.forms:
            await form.prepare()

    def should_delete_form(self, form: Form[Any]) -> bool:
        return self.can_delete and bool(form.validated_data.get(DELETION_FIELD_NAME))

    @property
    def deleted_forms(self) -> list[Form[Any]]:
        return (
            [form for form in self.forms if self.should_delete_form(form)] if self._valid() else []
        )

    @property
    def ordered_forms(self) -> list[Form[Any]]:
        if not self.can_order or not self._valid():
            raise AttributeError("ordered_forms is available on valid ordered formsets.")
        return sorted(
            (
                form
                for form in self.forms
                if form.has_changed() or form in self.initial_forms
                if not self.should_delete_form(form)
            ),
            key=lambda form: (
                form.validated_data.get(ORDERING_FIELD_NAME) is None,
                form.validated_data.get(ORDERING_FIELD_NAME) or 0,
            ),
        )

    @property
    def cleaned_data(self) -> list[dict[str, Any]]:
        if not self._valid():
            raise AttributeError("cleaned_data is available after successful validation.")
        return [form.validated_data for form in self.forms]

    def _valid(self) -> bool:
        return (
            self.is_bound and self._validated and not self._non_form_errors and not any(self.errors)
        )

    async def is_valid(self) -> bool:
        async with self._validation_lock:
            if not self._validated:
                await self.full_clean()
        return self._valid()

    async def full_clean(self) -> None:
        self._validated = False
        self.errors, self._non_form_errors = [], []
        if not self.is_bound:
            self._validated = True
            return
        await self.prepare()
        if not await self.management_form.is_valid():
            self._non_form_errors.append(
                self.error_messages["missing_management_form"]
                % {
                    "field_names": ", ".join(
                        self.management_form.add_prefix(name)
                        for name in self.management_form.errors
                    ),
                }
            )
        empty, deleted = 0, 0
        for index, form in enumerate(self.forms):
            await form.full_clean()
            if self.should_delete_form(form):
                deleted += 1
            else:
                self.errors.append(form.errors)
                empty += index >= self.initial_form_count() and not form.has_changed()
        try:
            if self._count(TOTAL_FORM_COUNT) > self.absolute_max or (
                self.validate_max and self.total_form_count() - deleted > self.max_num
            ):
                raise ValidationError(
                    self.error_messages["too_many_forms"], params={"num": self.max_num}
                )
            if self.validate_min and self.total_form_count() - deleted - empty < self.min_num:
                raise ValidationError(
                    self.error_messages["too_few_forms"], params={"num": self.min_num}
                )
            await invoke(self.clean)
        except ValidationError as exc:
            self._non_form_errors.extend(exc.messages)
        self._validated = True

    def clean(self) -> Any:
        pass

    def non_form_errors(self) -> list[str]:
        return self._non_form_errors

    def total_error_count(self) -> int:
        return len(self._non_form_errors) + sum(len(errors) for errors in self.errors)

    def has_changed(self) -> bool:
        return any(form.has_changed() for form in self.forms)

    def as_p(self) -> Markup:
        return self.management_form.as_p() + Markup("").join(form.as_p() for form in self.forms)


def formset_factory(
    form: type[Form[Any]],
    formset: type[BaseFormSet] = BaseFormSet,
    *,
    extra: int = 1,
    can_order: bool = False,
    can_delete: bool = False,
    max_num: int | None = None,
    validate_max: bool = False,
    min_num: int | None = None,
    validate_min: bool = False,
    absolute_max: int | None = None,
    can_delete_extra: bool = True,
) -> type[BaseFormSet]:
    maximum = DEFAULT_MAX_NUM if max_num is None else max_num
    absolute = maximum + DEFAULT_MAX_NUM if absolute_max is None else absolute_max
    minimum = 0 if min_num is None else min_num
    if (
        any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (extra, minimum, maximum, absolute)
        )
        or absolute < maximum
    ):
        raise ValueError("Form counts must be non-negative integers and absolute_max >= max_num.")
    return type(
        form.__name__ + "FormSet",
        (formset,),
        {
            "form": form,
            "extra": extra,
            "min_num": minimum,
            "max_num": maximum,
            "absolute_max": absolute,
            "can_order": can_order,
            "can_delete": can_delete,
            "can_delete_extra": can_delete_extra,
            "validate_min": validate_min,
            "validate_max": validate_max,
        },
    )


async def all_valid(formsets: Any) -> bool:
    return all([await formset.is_valid() for formset in formsets])
