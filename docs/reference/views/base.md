# Base views

Import the base classes from `obox.views.generic`:

```python
from obox.views.generic import TemplateView, View
from obox.views.generic.base import ContextMixin, TemplateResponseMixin
```

## `View`

`View` is the base for the built-in HTML views. Define a handler method for each
HTTP method the view accepts.

| Attribute or method | Description |
| --- | --- |
| `http_method_names` | Candidate handlers: `get`, `post`, `put`, `patch`, `delete`, `head`, `options`, and `trace`. A method is allowed only when the view provides that handler. |
| `dependencies` | FastAPI dependencies applied to routes created with `as_route()`. Defaults to an empty sequence. |
| `principal_dependency` | Dependency that resolves the request principal. Defaults to Obox identity resolution. |
| `csrf_protect` | Whether dispatch checks CSRF for unsafe requests. Defaults to `True`. |
| `check()` | Validate effective configuration when the view endpoint is created. Override and call `super().check()` when composing checks. |
| `setup(request, *args, **kwargs)` | Set `request`, positional `args`, and URL `kwargs` before dispatch. It maps `HEAD` to `get` when no `head` handler exists. |
| `dispatch(request, *args, **kwargs)` | Call the handler matching the lower-case request method. |
| `allowed_methods()` | Return supported method names in upper case. |
| `http_method_not_allowed(...)` | Return status `405` and the `Allow` header. |
| `options(...)` | Return an empty response with the `Allow` header. |
| `reverse(name, **params)` | Resolve a named FastAPI route, including the ASGI root path. |
| `redirect(name_or_path, **params)` | Redirect to a path or named route with status `303`. |

`as_view(**initkwargs)` returns a FastAPI endpoint. Initialization keywords must
name existing class attributes and cannot replace HTTP handler names. Obox
creates a new instance for each request; do not use class attributes as
request-local storage.

`as_route(router, path, *, name, initkwargs=None, dependencies=())` adds the
endpoint to an `APIRouter`. HTML routes are excluded from OpenAPI and use the
HTML error handler. Attach authorization dependencies when a standalone route
needs them.

## `ContextMixin`

`ContextMixin` supplies `extra_context` and `get_context_data(**kwargs)`. The
base context contains `view` and `principal`, followed by the supplied keyword
values and `extra_context`. Override the method to add data; pass it through the
inheritance chain with `super()` when combining mixins.

## `TemplateResponseMixin`

| Attribute or method | Description |
| --- | --- |
| `template_name` | Template to render. Required unless `get_template_names()` is overridden. |
| `get_template_names()` | Return the candidate template names. |
| `render_to_response(context, **response_kwargs)` | Select a candidate template and render it with the request and context. |

Obox uses the templates configured on its runtime. A standalone FastAPI app can
provide `app.state.templates`; otherwise Obox creates a Jinja environment from
the first configured `TEMPLATES` entry. The render context always contains
`request`. CSRF token data is added for HTML forms.

## `TemplateView`

`TemplateView` combines `TemplateResponseMixin`, `ContextMixin`, and `View`. Its
`get()` handler renders the selected template using `get_context_data()`.

```python
class AboutView(TemplateView):
    template_name = "pages/about.html"
    extra_context = {"title": "About"}
```
