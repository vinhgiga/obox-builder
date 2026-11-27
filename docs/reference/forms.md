# Forms

Obox forms combine typed fields with Pydantic validation. Validation and model
saving are asynchronous. Import the public form API from `obox.forms`.

```python
from pydantic import BaseModel
from obox.forms import Form


class FeedbackInput(BaseModel):
    email: str
    message: str


class FeedbackForm(Form[FeedbackInput]):
    schema = FeedbackInput


form = FeedbackForm({"email": "reader@example.com", "message": "Hello"})


async def submit_feedback() -> None:
    if await form.is_valid():
        await save_feedback(form.cleaned_data)
```

## `Form`

Set `schema` to a Pydantic model to define and validate the form's input fields.
`field_options` can provide or override field instances by Python field name.
`widgets`, `labels`, and `help_texts` customize the corresponding field
properties.

| Constructor option | Default | Description |
| --- | --- | --- |
| `data` | `None` | Submitted field values. Passing a mapping binds the form. |
| `files` | `None` | Uploaded files. Passing a mapping also binds the form. |
| `schema` | class `schema` | Pydantic model for this form instance. |
| `initial` | `{}` | Initial values for fields. |
| `prefix` | `None` | Prefix added to submitted field names. |
| `empty_permitted` | `False` | Allow a bound extra form with no changed fields to validate as empty. |

Form fields are copied for each instance. The main validation and rendering API
is:

| Attribute or method | Description |
| --- | --- |
| `fields` | Per-instance mapping from field names to field objects. |
| `is_bound` | Whether input data or files were supplied. |
| `is_valid()` | Async validation. Returns `False` for unbound forms. |
| `cleaned_data` | Validated Pydantic model, or `None` when validation fails. |
| `validated_data` | Mapping of converted field values. Read after validation. |
| `errors` | Mapping from field names to error message lists. |
| `non_field_errors()` | Errors not associated with one field. |
| `add_error(field, error)` | Add a field or non-field validation error. |
| `has_error(field, code=None)` | Check whether a field has an error, optionally by code. |
| `changed_data`, `has_changed()` | Fields changed from their initial values. |
| `prepare()` | Async preparation hook for fields that load choices or related data. |
| `is_multipart()` | Whether any field requires a multipart HTML form. |
| `as_p()` | Render the form fields as paragraph markup. |
| `form[name]` | Return a `BoundField` for rendering and error access. |

Generic editing views prepare forms before rendering or validating them. When
using a form directly, call `await form.prepare()` if its fields need prepared
choices or model relationships. Always await validation before reading errors
or cleaned values.

## Fields

Every field accepts the common options `required`, `label`, `initial`,
`help_text`, `widget`, `disabled`, `validators`, and `error_messages`. Validators
may be synchronous or asynchronous. `clean(value)` converts the submitted value,
checks requiredness, and runs validators.

| Field | Purpose and field-specific options |
| --- | --- |
| `Field` | Base conversion and validation behavior. |
| `CharField` | Text input; supports `min_length`, `max_length`, and whitespace stripping. |
| `IntegerField` | Integer input with optional `min_value` and `max_value`. |
| `DecimalField` | Decimal input with optional `max_digits` and `decimal_places`. |
| `FloatField` | Finite floating-point input. |
| `BooleanField` | Checkbox value converted to `bool`. |
| `DateField`, `DateTimeField`, `TimeField` | Typed date and time values. Naive datetimes use `TIME_ZONE` when `USE_TZ` is enabled and settings are configured. |
| `UUIDField` | UUID input. |
| `JSONField` | JSON text converted to Python data. |
| `ChoiceField` | Value selected from `choices`. |
| `FileField` | Uploaded file input; `FileInput(max_size=...)` can limit the upload size. |
| `ModelChoiceField` | One model object selected from a SQLAlchemy query. |
| `ModelMultipleChoiceField` | Multiple model objects selected from a SQLAlchemy query. |

`ModelChoiceField` accepts a `queryset`, supports `label_from_instance(obj)`,
and validates submitted keys against that query. Its multiple-choice subclass
returns a list of matching objects.

## Widgets

Widgets implement `render(name, value, attrs=None, renderer=None)` and return
escaped HTML markup.

| Widget | HTML control | Notes |
| --- | --- | --- |
| `Widget`, `TextInput` | text input | Base widget and default control. |
| `NumberInput` | number input | Used by numeric fields. |
| `HiddenInput` | hidden input | Used for hidden values and formset controls. |
| `PasswordInput` | password input | Does not render its value unless `render_value=True`. |
| `FileInput` | file input | Marks the form as multipart; optional `max_size` is in bytes. |
| `CheckboxInput` | checkbox | Used by boolean fields. |
| `Textarea` | textarea | Defaults to 40 columns and 10 rows. |
| `Select`, `SelectMultiple` | select control | Accept a sequence of `(value, label)` choices. |

## `ModelForm`

`ModelForm` maps a SQLAlchemy model to a form. Define `Meta.model` and either
`Meta.fields` or `Meta.exclude`; a form must not leave both unspecified. You may
also provide a Pydantic `schema` for validation.

```python
from decimal import Decimal
from typing import Any, ClassVar
from obox import forms
from pydantic import BaseModel

from .models import Product


class ProductInput(BaseModel):
    name: str
    price: Decimal


class ProductForm(forms.ModelForm[ProductInput, Product]):
    schema = ProductInput

    class Meta:
        model = Product
        fields = ("name", "price")
        labels: ClassVar[dict[str, str]] = {"name": "Product name"}
        widgets: ClassVar[dict[str, Any]] = {
            "name": forms.TextInput(attrs={"placeholder": "Name"})
        }
```

`Meta` also accepts `exclude`, `help_texts`, `widgets`, and `labels`. Other keys
raise `ImproperlyConfigured` when the form class is defined. Declared
fields can customize generated model fields. Editable scalar columns and
supported relationships are mapped to form fields; unsupported model column
types raise a configuration error.

| Method | Description |
| --- | --- |
| `prepare()` | Load missing relationship values needed by the form. |
| `is_valid()` | Validate the form and model-level constraints asynchronously. |
| `save(commit=True)` | Persist through the shared resource behavior and return the instance. |
| `save(commit=False)` | Apply cleaned values to the instance without persisting it. |
| `save_m2m()` | Persist supported relationship values after the instance has been saved. |

Validation does not persist data or mutate an existing instance. Use
`async with atomic()` from `obox.db` when coordinating the form save with other
database operations.

## Form factories

| Factory | Purpose and notable options |
| --- | --- |
| `fields_for_model(model, fields=None, exclude=None, ...)` | Build field instances from editable model columns and relationships. |
| `modelform_factory(model, form=ModelForm, *, fields=None, exclude=None, widgets=None, labels=None, help_texts=None, schema=None)` | Create a `ModelForm` class for one model. |
| `formset_factory(form, formset=BaseFormSet, *, extra=1, can_order=False, can_delete=False, max_num=None, validate_max=False, min_num=None, validate_min=False, absolute_max=None, can_delete_extra=True)` | Create a collection form class with bounded form counts. |
| `modelformset_factory(model, form=ModelForm, *, fields=None, exclude=None, widgets=None, labels=None, help_texts=None, edit_only=False, ...)` | Create a model-backed formset class. |
| `inlineformset_factory(parent_model, model, form=ModelForm, *, formset=BaseInlineFormSet, fk_name=None, extra=3, can_delete=True, ...)` | Create a model formset joined to one parent model. |

Formsets require a management form with `TOTAL_FORMS` and `INITIAL_FORMS`.
`is_valid()` validates the management data, each form, and configured minimum or
maximum counts. `deleted_forms`, `ordered_forms`, and `cleaned_data` are
available after successful validation. `all_valid(formsets)` asynchronously
validates each supplied formset.
