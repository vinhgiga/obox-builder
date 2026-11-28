# Python API

Use the [class-based view reference](views/index.md), [Model REST API](api.md),
[forms reference](forms.md), and [settings reference](settings.md) for behavior,
configuration, and extension hooks. This page summarizes the import paths and
registration signatures.

| Import | Purpose |
| --- | --- |
| `from obox import Obox, ModelResource, permission_required, setup` | FastAPI adapter, shared CRUD, route authorization and app loading |
| `from obox.apps import AppConfig` | Installed app configuration and declarative registration |
| `from obox.routes import Route` | HTML route declarations that select templates and permissions |
| `from obox.components import Component` | Reusable component declarations |
| `from obox.views.generic import ModelViewSet, GridView, ViewConfig, DetailView, CreateView, UpdateView, DeleteView, FormView, TemplateView` | Generated and standalone HTML views |
| `from obox.api import ModelApi, ApiRequest, ApiResponse, Context, api_context` | Native FastAPI REST adapter and schemas |
| `from obox import forms` | Typed HTML forms, fields and widgets |
| `from obox.db import atomic` | Async transaction boundary |

`Obox.register(target, *, slug=None, api=True, views=True)`
registers a mapped SQLAlchemy class or a `ModelResource` subclass. `api` accepts
`True`, `False` or a `ModelApi` subclass. `views` accepts `True`, `False` or a
`ModelViewSet` subclass. `AppConfig.register()` accepts the
same arguments from `ready()` and prefixes URLs with the app's label. By
default, HTML uses the model class name in lowercase kebab case, while REST
uses the SQLAlchemy table name under `API_PREFIX`. `slug` overrides the route
suffix; adapter `prefix` attributes can override it per surface.

`AppConfig` also declares `views` and `apis` sequences containing `ModelResource`
classes, `ModelViewSet`/`ModelApi` subclasses, or dotted paths. `components`,
`routes`, `routers`, `requires`, `ui_prefix`, `view_prefix` and `api_prefix`
configure the rest of the application.

`ModelResource` accepts a mapped `model`, optional Pydantic `write_schema`,
`fields`, `readonly_fields`, `ordering`, `page_size`, `lookup_field` and
`load_options`. Set `search_fields`, `ordering_fields`, `filter_fields` or
`group_fields` to constrain collection queries on both adapters. Override
`get_queryset()` for scoped reads and `can()` and `get_writable_fields()` for
write policy. `prepare_write()` can supply trusted values after validation.

`ModelViewSet` groups a `resource` with `slug`, `prefix` and custom
`list_view`, `detail_view`, `create_view`, `update_view` and `delete_view` classes.
`GridView` accepts `fields`, `page_size`, and `view`. Use `template_name` for
a custom layout. `CreateView` and `UpdateView` accept `form_class` and `fieldsets`.

`ModelApi` accepts `resource`, `prefix`, `slug`, `actions`, `permissions`,
`dependencies`, `default_limit` and `max_limit`. Call
`ProductApi.as_router("/v0/product")` to mount a custom adapter.

`AppConfig.routes` maps static paths to templates. A route may set `title`,
`permission`, `menu`, and a custom `TemplateView` subclass in `view`. Templates
declare components at their use site with `{{ component("package.module.Class") }}`.
The supported literal options are `key`, `title`, and `filters`.

Use `Depends(permission_required("app.action_model", html=True))` on HTML
routes and omit `html=True` for REST routes. The dependency checks the named
permission and handles CSRF on unsafe cookie-authenticated requests.
