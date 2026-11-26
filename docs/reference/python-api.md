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
| `from obox.views.generic import ModelViewSet, ListView, DetailView, CreateView, UpdateView, DeleteView, FormView, TemplateView, StackedInline, TabularInline, action` | Generated and standalone HTML views |
| `from obox.api import ModelApi, ApiRequest, ApiResponse, Context` | Native FastAPI REST adapter and schemas |
| `from obox import forms` | Fields, widgets, forms, and formset factories |
| `from obox.db import atomic` | Async transaction boundary |

`Obox.register(target, *, slug=None, api=True, views=True, list_view=None, detail_view=None, create_view=None, update_view=None, delete_view=None)`
registers a mapped SQLAlchemy class or a `ModelResource` subclass. `api` accepts
`True`, `False` or a `ModelApi` subclass. `AppConfig.register()` accepts the
same arguments from `ready()` and prefixes URLs with the app's label.

`AppConfig` also declares `views` and `apis` sequences containing `ModelResource`
classes, `ModelViewSet`/`ModelApi` subclasses, or dotted paths. `components`,
`routes`, `routers`, `requires`, `ui_prefix`, `view_prefix` and `api_prefix`
configure the rest of the application.

`ModelResource` accepts a mapped `model`, optional Pydantic `schema`,
`create_schema`, `update_schema`, `fields`, `readonly_fields`, `ordering`,
`page_size`, `lookup_field` and `load_options`. Override `get_queryset(self)`
(the bound principal is `self.principal`), `can(action, obj, actor)`,
`get_writable_fields(action, obj, actor)` or `prepare_write(values, obj, actor)`.
The write hooks receive the same inputs for both HTML and REST. `prepare_write`
runs after input validation and can supply trusted server values such as an
owner ID.

`ModelViewSet` groups a `resource` with `slug`, `prefix` and custom
`list_view`, `detail_view`, `create_view`, `update_view` and `delete_view`
classes. `ListView` options include `list_display`, `list_display_links`,
`list_filter`, `search_fields`, `paginate_by` and `actions`. `CreateView` and
`UpdateView` add `fields`, `fieldsets`, `inlines` and `form_class`.

`ModelApi` options include `resource` or `model`, `prefix`, `slug`,
`include_router_methods`, `permissions`, `read_schema`, `create_schema`,
`update_schema`, `patch_schema`, `dependencies`, `search_fields`,
`ordering_fields`, `default_limit` and `max_limit`. Call
`ModelApi().build_router("/api/products")` to mount it directly.

`AppConfig.routes` maps static paths to templates. A route may set `title`,
`permission`, `menu`, and a custom `TemplateView` subclass in `view`. Templates
declare components at their use site with `{{ component("package.module.Class") }}`.
The supported literal options are `key`, `title`, and `filters`.

Use `Depends(permission_required("app.action_model", html=True))` on HTML
routes and omit `html=True` for REST routes. The dependency checks the named
permission and handles CSRF on unsafe cookie-authenticated requests.
