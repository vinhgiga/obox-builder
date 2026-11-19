# Views

[Generic display/editing views](list-and-filters.md) with per-request instances and Select querysets.

## File forms

Declare the file as an `UploadFile` and call an application service from
`form_valid()`. The service can parse and validate CSV rows and write the
contacts in a transaction.

```python
from typing import Any, cast

from fastapi import Request, Response, UploadFile

from obox import forms
from obox.core.exceptions import PermissionDenied
from obox.views.generic import FormView

from .services import import_contacts


class ContactImportForm(forms.Form):
    csv_file: UploadFile = forms.field(
        forms.FileInput({"accept": ".csv,text/csv"}, max_size=5 * 1024 * 1024)
    )


class ContactImportView(FormView):
    form_class = ContactImportForm
    template_name = "obox/generic/form.html"
    success_url = "/contacts/"

    async def dispatch(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        if not request.user.has_perm("crm.contact.import"):
            raise PermissionDenied()
        return cast(Response, await super().dispatch(request, *args, **kwargs))

    async def form_valid(self, form: forms.Form) -> Response:
        await import_contacts(cast(ContactImportForm, form).csv_file)
        return await super().form_valid(form)
```

This form accepts files up to 5 MiB. The `accept` attribute guides the browser's
file picker. Validate the file contents in your application. Grant
`crm.contact.import` through your application's permission policy. The view closes
the upload when it exits.
Custom templates can set the form encoding with `form.is_multipart()` and
include the usual `csrfmiddlewaretoken` field.

For a shared site URL, mount the view in `ROOT_URLCONF`. Use an empty route for
the home page or configure the view with a custom home template.

```python
from obox.urls import path

from contacts.views import ContactImportView

urlpatterns = [
    path("contact/import", ContactImportView.as_view(), name="contact-import"),
]
```

For an app-specific page, put `path("import", ContactImportView.as_view(),
name="import")` in the app's URLconf, set `app_name = "contacts"`, and mount it
with `path("crm/contact/", include("contacts.urls"))`. Reverse the route as
`contacts:import`.

For a button on the contact list, extend `obox/generic/list.html` or set the
builder's `change_list_template` to a template extending
`obox/admin/change_list.html`. Add a link to the chosen route in `object_tools`.

```jinja
{% extends "obox/generic/list.html" %}
{% block object_tools %}
  {{ super() }}
  {% if request.user.has_perm("crm.contact.import") %}
    <a href="{{ request.url_for('contact-import') }}">Import contacts</a>
  {% endif %}
{% endblock %}
```
