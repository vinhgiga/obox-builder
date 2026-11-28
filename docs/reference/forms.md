# Forms

Subclass `Form[S]` and set `schema` to a Pydantic model. Pydantic validates the
submitted values, while the form binds input to controls and renders field errors.

```python
from obox.forms import Field as Control, Form, Textarea
from pydantic import BaseModel, Field


class FeedbackInput(BaseModel):
    email: str
    message: str = Field(min_length=1, max_length=1000)


class FeedbackForm(Form[FeedbackInput]):
    schema = FeedbackInput
    field_options = {"message": Control(widget=Textarea, label="Your feedback")}
```

Pass `data` and `files` to bind a form, `initial` for display values, and `prefix`
to namespace its controls. The constructor also accepts an explicit `schema`.
Controls are copied per form instance. Set `field_options` to override a
schema field's widget, label, help text, initial value, or disabled state.

Await `form.is_valid()` before reading `form.cleaned_data`. Successful validation
sets `cleaned_data` to an instance of the declared schema. Use `form.errors`,
`form.errors_for(name)`, and `form.non_field_errors()` to display errors. Iterate
the form or use `form[name]` to render its bound controls. The raw submitted
values remain available for rendering when validation fails.

## Controls

| Control | Use |
| --- | --- |
| `Field` | Text input or a configured widget |
| `BooleanField` | Checkbox. A missing enabled control submits `False` |
| `ChoiceField` | Selection from a list of label/value pairs |
| `JSONField` | JSON input |
| `FileField` | Multipart file input |

`TextInput`, `NumberInput`, `HiddenInput`, `PasswordInput`, `Textarea`,
`CheckboxInput`, `Select`, `SelectMultiple`, and `FileInput` render escaped HTML.
Choose `FileInput(max_size=...)` on a `FileField` to limit the upload size in
bytes. `FormView` closes uploaded files after the response. See the
[upload example](../examples/views/index.md#file-forms).

## Model editing

`CreateView` and `UpdateView` choose the schema and editable fields from their
`ModelResource`. Configure `write_schema` on the Resource and `form_class` on
the view to customize HTML controls. The views pass input to `resource.create`
or `resource.update`, which validate and write in the current unit of work.
Use these Resource methods when writing records from application code.
