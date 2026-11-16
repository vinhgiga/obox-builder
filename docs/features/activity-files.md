# Activity and files

These optional contrib apps operate on registered contrib resources. Enable
resources, auth and required features through INSTALLED_APPS, for example:

```python
INSTALLED_APPS = [
    "obox.contrib.resources",
    "obox.contrib.auth",
    "obox.contrib.files",
    "obox.contrib.activity",
    "app.products",
]
MEDIA_ROOT = "uploads"
FILES = {"root": MEDIA_ROOT}
```

Activity records revisions and exposes plain-text comments, author ownership,
cursor pagination and versioned edits. Parent resource policy applies to all
nested access. Include feature tables in application migrations.

Files supports resumable upload, attachment ownership/claiming, MIME/size/count
rules and authorized downloads. Attachment collections use `attachments()` with
a mapped list of File records. The resource editor retains its existing product
validation and concurrency behavior. Scalar core ModelForm/ModelSerializer do
not automatically expose attachment relationships.

Read [record/activity](../record-activity-spec.md) and
[resource protocol](../contrib-resource-api.md) for endpoint details.
