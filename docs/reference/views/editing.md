# Editing views

`FormView`, `CreateView`, `UpdateView`, and `DeleteView` handle HTML input.
Standalone forms validate through [Forms](../forms.md). Registered model
views write through their Resource.

## `FormMixin`

| Attribute or method | Default | Description |
| --- | --- | --- |
| `form_class` | `None` | Form class to instantiate. |
| `schema` | `None` | Pydantic schema used when a form class is not supplied. |
| `initial` | `{}` | Initial field values, copied for each request. |
| `prefix` | `None` | Prefix for submitted field names. |
| `success_url` | `None` | Path, absolute URL, or named route used after a valid form. |
| `invalid_status_code` | `422` | Status returned when rendering invalid input. |
| `get_initial()` | — | Return initial values for the request. |
| `get_form_kwargs()` | — | Build constructor arguments, including submitted form data. |
| `get_form()` | — | Construct and prepare the form. |
| `get_success_url()` | — | Resolve the configured success target. |
| `form_valid(form)` | — | Handle valid input; the base implementation redirects with status `303`. |
| `form_invalid(form)` | — | Render the form and errors. |

## `FormView`

`FormView` renders a form on `GET`. On `POST`, it prepares the form,
awaits `form.is_valid()`, then calls `form_valid()` or `form_invalid()`. The base
`form_valid()` redirects; override it to run application behavior or persist
data. A `success_url` is required unless the view supplies its own success
handling.

```python
class ContactView(FormView):
    form_class = ContactForm
    template_name = "contact.html"
    success_url = "contact-thanks"

    async def form_valid(self, form):
        await send_contact_message(form.cleaned_data)
        return await super().form_valid(form)
```

Invalid forms render with status `422` by default. Set `invalid_status_code` to
change that status.

## Model editing

`CreateView` and `UpdateView` use their Resource's `write_schema` and writable
fields. Supply a `form_class` to customize controls and `fieldsets` to arrange
the fields in the default template. Put business logic in the Resource or its
service. The view redisplays errors with status 422 and redirects with 303 on
success.

`DeleteView` renders a confirmation on GET and deletes on POST. It uses the
same Resource scope and action permission as REST. Standalone model views
require `success_url` or a `get_success_url()` override. Registered views
default to the collection URL.
