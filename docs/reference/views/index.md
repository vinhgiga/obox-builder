# Built-in class-based views

Obox provides per-request HTML views for FastAPI applications. The built-in
views render Jinja templates and use native Starlette requests and responses.
They are separate from the generated JSON routes documented in the
[Model REST API](../api.md).

## View classes

| Class | Purpose |
| --- | --- |
| `View` | Dispatch a request to a method handler. |
| `TemplateView` | Render a template with context data. |
| `ListView` | Query, filter, and paginate a collection. |
| `DetailView` | Load one record and render its detail page. |
| `FormView` | Validate a form and run custom success behavior. |
| `CreateView` | Create a model record from a model form. |
| `UpdateView` | Edit a model record with a model form. |
| `DeleteView` | Confirm and delete a model record. |
| `ModelViewSet` | Group a `Resource` and its HTML view classes for registration. |

See [base views](base.md), [display views](display.md),
[editing views](editing.md), and [ModelViewSet](model-viewset.md).

## Request lifecycle

`View.as_view()` validates its initialization options and calls `check()` when
the route is created. It returns an endpoint that creates a new view instance
for each request, calls `setup()`, opens the request session, binds the active
principal, and dispatches to the method matching the HTTP verb. A `GET` handler
also handles `HEAD` unless the class defines its own `head()` method. Unsupported
methods return `405` with an `Allow` header.

View handlers and hooks may be synchronous or asynchronous. Database operations
use the active asynchronous SQLAlchemy session. The request, path arguments,
principal, and session are available as `self.request`, `self.args`,
`self.kwargs`, `self.principal`, and `self.session` during dispatch.

HTML views protect unsafe requests with CSRF by default. Set
`csrf_protect = False` only for a view that does not accept browser credentials
or mutate state. For registered Obox routes, CSRF tokens and template context
are prepared by the runtime.

## Registration

Use `Obox.register()` or an installed `AppConfig` to create model routes:

```python
class ProductList(ListView):
    list_display = ("name", "price")


obox.register(Product, list_view=ProductList)
```

For a standalone FastAPI route, use `as_view()` and attach the dependencies that
protect that route:

```python
app.add_api_route(
    "/about",
    AboutView.as_view(),
    methods=["GET"],
    include_in_schema=False,
)
```

`View.as_route(router, path, name=..., initkwargs=..., dependencies=...)` is a
convenience for adding a view to an `APIRouter`. It infers the allowed methods,
adds `HEAD` when appropriate, and excludes HTML routes from the generated API
schema. For a standalone route that requires authorization, attach a dependency
such as `Depends(permission_required("app.view_page", html=True))`.
