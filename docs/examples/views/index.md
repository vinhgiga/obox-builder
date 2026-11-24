# Views

[Generic display/editing views](list-and-filters.md) with per-request instances and Select querysets.

## File forms

Declare the file in the form's Pydantic schema as `UploadFile` and call an
application service from `form_valid()`. The service can parse and validate CSV
rows and write the contacts in a transaction.

```python
from typing import Any, ClassVar, cast

from fastapi import Response, UploadFile
from pydantic import BaseModel, ConfigDict

from obox import forms
from obox.views.generic import FormView

from .services import import_contacts


class ContactImportInput(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    csv_file: UploadFile


class ContactImportForm(forms.Form[ContactImportInput]):
    schema = ContactImportInput
    widgets: ClassVar[dict[str, Any]] = {
        "csv_file": forms.FileInput({"accept": ".csv,text/csv"}, max_size=5 * 1024 * 1024)
    }


class ContactImportView(FormView):
    form_class = ContactImportForm
    template_name = "obox/generic/form.html"
    success_url = "/contacts/"

    async def form_valid(self, form: forms.Form) -> Response:
        upload = cast(ContactImportForm, form).cleaned_data.csv_file
        await import_contacts(upload)
        return await super().form_valid(form)
```

This form accepts files up to 5 MiB. The `accept` attribute guides the browser's
file picker. Validate the file contents in your application. Grant
`crm.contact.import` through your application's permission policy. The view closes
the upload when it exits.
Custom templates can set the form encoding with `form.is_multipart()` and
include the usual `csrfmiddlewaretoken` field.

Attach the file form as a native FastAPI route with its permission.

```python
from fastapi import Depends
from obox import permission_required

app.add_api_route(
    "/contacts/import",
    ContactImportView.as_view(),
    methods=["GET", "POST"],
    dependencies=[Depends(permission_required("crm.contact.import", html=True))],
    include_in_schema=False,
)
```
