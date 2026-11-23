# Activity and files

Activity and Files are optional contrib apps. Enable the features your host uses
through `INSTALLED_APPS`, for example:

```python
INSTALLED_APPS = [
    "obox.contrib.auth",
    "obox.contrib.files",
    "obox.contrib.activity",
    "app.products",
]
MEDIA_ROOT = "uploads"
FILES = {"root": MEDIA_ROOT}
# Article is an ORM model declared by app.products.
ACTIVITY = {"models": {Article: ("title", "status")}}
```

Activity records changes to configured ORM fields in the caller's transaction
and exposes plain-text comments, cursor pagination and versioned edits. Include
its feature tables in application migrations.

Files supports resumable upload, attachment ownership/claiming, MIME/size/count
rules and authorized downloads. Attachment collections use `attachments()` with
a mapped list of `File` records. Include their feature tables in application
migrations and grant `files.access_files` alongside model permissions.

Read the [record activity contract](../record-activity-spec.md) and the
[file attachment example](../examples/features/file-attachments.md) for details.
