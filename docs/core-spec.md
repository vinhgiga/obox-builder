# Core contract — v0.1.0

The reference is Django 5.2 LTS and DRF 3.16.1. Obox supports the subset listed in
[the mapping table](mapping.md). Runtime models are native SQLAlchemy models,
requests are native Starlette/FastAPI requests, and templates use Jinja2.
`Obox(app)` is the FastAPI adapter; `ModelBuilder` names the supported ModelAdmin subset.

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
with status 200; successful editing redirects with 302; delete uses POST after
confirmation. Unsupported methods return 405 and an Allow header.

`path()` and `include()` mount nested URL patterns, converter values and named
namespaces on FastAPI. Reverse links use the host request's `url_for()`.
HTML unsafe methods require the `csrftoken` cookie plus `csrfmiddlewaretoken`
or `X-CSRFToken`. Origin/referer checks apply. Jinja2 context exposes `csrf_token`.
Generic editing views use FastAPI's request form parsing, bind multipart text and
files separately, and close uploads when the view exits. Built-in form templates
set the encoding automatically.
See [File forms](examples/views/index.md#file-forms) for routing and list integration.

## ModelBuilder

Register `(model, builder_class)` on an AdminSite or use `@register(Model)`.
The builder accepts `form`, `fields`, `fieldsets`, `readonly_fields`,
`list_display`, `list_filter`, `search_fields`, `ordering`, `list_per_page` and
admin template/action options, including list_display_links. It exposes ModelAdmin-shaped form/queryset,
save/delete, action and permission hooks.

AdminSite enforces active staff access. Model permissions use
`app_label.add_model`, `change_model`, `delete_model` and `view_model`.
Builder querysets are Select objects; save/delete hooks are awaited. It generates
admin HTML routes only. Changelists expose search/filter controls, actions and
Django admin page numbering (`p=0` for the first page). See [REST](api-spec.md) for independent endpoint registration.

## Optional product features

`obox.contrib.resources` owns product resource declarations, policy context,
resource services, batch/upsert, metadata, version checks and inline editing.
Other contrib apps build on those services. Their custom methods, response
conventions and product options do not extend the Django/DRF core contract.
See [the resource protocol](contrib-resource-api.md) and
[record/activity protocol](record-activity-spec.md).

Feature-named tests under `tests/` cover the core. Existing product regression tests
cover contrib. Reference tests run Django/DRF in a separate pinned environment;
matching the documented subset does not promise full framework compatibility.
