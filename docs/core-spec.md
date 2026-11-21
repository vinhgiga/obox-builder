# Core contract — v0.1.0

The reference is Django 5.2 LTS and DRF 3.16.1. Obox supports the subset listed in
[the mapping table](mapping.md). Runtime models are native SQLAlchemy models,
requests are native Starlette/FastAPI requests, and templates use Jinja2.
`Obox(app)` installs the FastAPI adapter. `Resource` shares model operations between HTML and REST.

## Settings and applications

`obox.conf.settings` loads uppercase settings lazily from `OBOX_SETTINGS_MODULE`,
or accepts a single `settings.configure(...)` call. Access before configuration
raises `ImproperlyConfigured`; configuring twice raises `RuntimeError`.

AppConfig has the Django constructor `(app_name, app_module)`, `create()`, `name`,
`label`, `verbose_name`, `path`, `import_models()`, `get_model()` and `ready()`.
Registry population runs all configs, then all model imports, then all ready hooks.
Population is idempotent and rejects duplicate names/labels and reentrant loading.
`obox.setup()` populates the global registry without installing HTTP routes.

`DATABASES['default']` supports SQLite and PostgreSQL. `TEMPLATES` accepts the
Obox Jinja2 backend, directories, application templates and environment options.
Database/schema management remains SQLAlchemy/Alembic; no QuerySet emulation exists.

## Forms

`Form` declares fields; `ModelForm` derives scalar fields from `Meta.model` and
explicit `Meta.fields` or `Meta.exclude`. Meta also supports `labels`, `widgets`
and `help_texts`. Per-instance input is `data`, `files`, `initial`, `prefix`, and,
for model forms, `instance`. Fieldsets belong to ModelBuilder.

Unbound forms are invalid and have no errors. Await `is_valid()` or `full_clean()`
before reading `errors` or `cleaned_data`. Cleaning runs field conversion and
validators, `clean_<field>()`, `clean()`, then model uniqueness/foreign-key checks.
`fastapi.UploadFile` declares a file input. Bind uploads with
`files=` using the same prefix rules as `data=`. Use `UploadFile | None` for an
optional file. `Form.is_multipart()` reports the encoding required by its widgets.
`FileInput(max_size=...)` sets an optional per-file byte limit, enforced during
form validation. Files larger than the limit receive a `max_size` field error.
`add_error()` removes the affected value from cleaned_data. Validation never saves
or mutates a persisted SQLAlchemy instance. This differs from Django's in-memory
model construction to avoid SQLAlchemy's automatic dirty tracking.

`await form.save(commit=False)` applies validated data to an instance without
inserting/updating it. `await form.save()` flushes within the current transaction
or owns a short transaction. `async with transaction.atomic()` groups work and
rolls back on exceptions. Metadata/configuration methods stay synchronous;
database-aware hooks may be asynchronous. Uploaded files are available to the
application during form processing. The application owns any file storage and
model persistence for upload fields.

## Generic views and URLs

Supported views are `View`, `TemplateView`, `ListView`, `DetailView`, `FormView`,
`CreateView`, `UpdateView` and `DeleteView`. Each request gets its own view and form.
The lifecycle is `as_view()` → `setup()` → `dispatch()` → method handler.

`get_queryset()` returns an unevaluated SQLAlchemy Select. `get_object()` applies
lookup conversion and executes it. Context includes Django-shaped `object`,
`object_list`, model context names, `form` and pagination values where applicable.
The usual context/template/form hooks can be overridden. Invalid forms render
with status 200; successful editing redirects with 303; delete uses POST after
confirmation. Unsupported methods return 405 and an Allow header.

Mount standalone views through FastAPI and supply the permitted HTTP methods
and permission dependency. Reverse links use the host request's `url_for()`.
HTML unsafe methods require the `csrftoken` cookie plus `csrfmiddlewaretoken`
or `X-CSRFToken`. Origin/referer checks apply. Jinja2 context exposes `csrf_token`.
Generic editing views use FastAPI's request form parsing, bind multipart text and
files separately, and close uploads when the view exits. Built-in form templates
set the encoding automatically.
See [File forms](examples/views/index.md#file-forms) for routing and list integration.

## Resource registration

Register a mapped class or a `Resource` on `Obox` or in `AppConfig.ready()`.
The registration builds the selected HTML and REST routes on FastAPI. The shared
resource owns visibility, input validation and write policy. Builder options
choose the form, views and API adapter. See [Resource registration](examples/features/register-resource.md).

Startup checks that routes have a permission policy. Model operations use
`app_label.view_model`, `add_model`, `change_model` and `delete_model`.
See [access control](features/access-control.md) for custom routes and role setup.
