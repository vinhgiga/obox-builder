# Python API

| Import | Purpose |
| --- | --- |
| `from obox import Obox, Resource, permission_required, setup` | Installation, shared CRUD, route authorization and app loading |
| `from obox.apps import AppConfig` | Installed app configuration and scoped registration |
| `from obox.routes import Route` | HTML route declarations that select templates and permissions |
| `from obox.components import Component` | Reusable component declarations |
| `from obox.builder import ModelBuilder, TabularInline, StackedInline, action` | Generated adapter options and HTML actions |
| `from obox.api import ModelApi, ApiRequest, ApiResponse, Context` | Native FastAPI REST adapter and schemas |
| `from obox.forms import Form, ModelForm, modelform_factory` | Standalone and model forms |
| `from obox.views.generic import ListView, DetailView, CreateView, UpdateView, DeleteView, TemplateView` | Standalone, routed template, or generated HTML views |

`AppConfig.routes` maps static paths to templates. A route may set `title`,
`permission`, `menu`, and a custom `TemplateView` subclass in `view`. Templates
declare components at their use site with `{{ component("package.module.Class") }}`.
The supported literal options are `key`, `title`, and `filters`.

`Obox.register(target, *, builder=None, api=True, views=True)` registers a
mapped SQLAlchemy class or a `Resource` subclass. `AppConfig.register()` accepts
the same arguments from `ready()` and prefixes URLs with the app's label.

`Resource` accepts a mapped `model`, optional Pydantic `schema`, `fields`,
`readonly_fields` and `ordering`. Override `get_queryset(actor)`,
`can(action, obj, actor)`, `get_writable_fields(action, obj, actor)` or
`prepare_write(values, obj, actor)`. The write hooks receive the same inputs
for both HTML and REST. `prepare_write` runs after input validation and can
supply trusted server values such as an owner ID.

`ModelBuilder` options include `slug`, `form`, `api_class` and
`list_view`, `detail_view`, `create_view`, `update_view`, `delete_view`.

Use `Depends(permission_required("app.action_model", html=True))` on HTML
routes and omit `html=True` for REST routes. The dependency checks the named
permission and handles CSRF on unsafe cookie-authenticated requests.
