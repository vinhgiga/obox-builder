# Core contract — v0.1.0

This document describes the supported core API behavior. Runtime models are
native SQLAlchemy models,
requests are native Starlette/FastAPI requests, and templates use Jinja2.
`Obox(app)` installs the FastAPI adapter. `ModelResource` shares model operations between HTML and REST.

## Settings and applications

`obox.conf.settings` loads uppercase settings lazily from `OBOX_SETTINGS_MODULE`,
or accepts a single `settings.configure(...)` call. Access before configuration
raises `ImproperlyConfigured`; configuring twice raises `RuntimeError`.

AppConfig accepts `(app_name, app_module)` and provides `create()`, `name`,
`label`, `verbose_name`, `path`, `import_models()`, `get_model()` and `ready()`.
Registry population runs all configs, then all model imports, then all ready hooks.
Population is idempotent and rejects duplicate names/labels and reentrant loading.
`obox.setup()` populates the global registry without installing HTTP routes.

`DATABASES['default']` supports SQLite and PostgreSQL. `TEMPLATES` accepts the
Obox Jinja2 backend, directories, application templates and environment options.
Database/schema management remains SQLAlchemy/Alembic; no QuerySet emulation exists.

## Forms

`Form[S]` binds and renders HTML controls defined by a Pydantic schema. Override a
control with `field_options`. Await `is_valid()` before reading `cleaned_data`.
Use `UploadFile` for multipart upload fields and `FileInput(max_size=...)` to
enforce a byte limit.

A Resource supplies `write_schema` for model editing and owns the write
transaction. HTML create/update views and REST create/patch send input to the
same Resource validation. Patch preserves all omitted fields.

## Generic views and URLs

Supported views are `View`, `TemplateView`, `GridView`, `DetailView`, `FormView`,
`CreateView`, `UpdateView` and `DeleteView`. Each request gets its own view and form.
The lifecycle is `as_view()` → `setup()` → `dispatch()` → method handler.

`ModelResource.get_queryset()` returns the scoped SQLAlchemy Select. `get_object()` applies
lookup conversion and executes it. Context includes `object`,
`object_list`, model context names, `form` and pagination values where applicable.
The usual context/template/form hooks can be overridden. Invalid forms render
with status 422. Successful editing redirects with 303. Delete uses POST after
confirmation. Unsupported methods return 405 and an Allow header.

Mount standalone views through FastAPI and supply the permitted HTTP methods
and permission dependency. Reverse links use the host request's `url_for()`.
HTML unsafe methods require the `csrftoken` cookie plus `csrfmiddlewaretoken`
or `X-CSRFToken`. Origin/referer checks apply. Jinja2 context exposes `csrf_token`.
Generic editing views use FastAPI's request form parsing, bind multipart text and
files separately, and close uploads when the view exits. Built-in form templates
set the encoding automatically.
See [File forms](examples/views/index.md#file-forms) for routing and list integration.

## ModelResource registration

Register a mapped class or a `ModelResource` on `Obox` or in `AppConfig.ready()`.
The registration builds the selected HTML and REST routes on FastAPI. The shared
resource owns visibility, input validation and write policy. Pass a `ModelViewSet` subclass as `views` and a `ModelApi` subclass as `api`
to customize the adapters. `AppConfig.views`
and `AppConfig.apis` support the same choices declaratively. See
[ModelResource registration](examples/features/register-resource.md).

Startup checks that routes have a permission policy. Model operations use
`app_label.view_model`, `add_model`, `change_model` and `delete_model`.
See [access control](features/access-control.md) for custom routes and role setup.
