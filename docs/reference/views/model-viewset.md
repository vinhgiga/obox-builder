# `ModelViewSet`

`ModelViewSet` binds one shared `Resource` to the built-in HTML routes. It is a
registration declaration, not a request handler. Use a `ModelViewSet` subclass
in an installed app's `views` sequence, or pass view classes to
`Obox.register()` directly.

```python
from obox.apps import AppConfig
from obox.views.generic import ListView, ModelViewSet

from .models import Product
from .resources import Products


class ProductList(ListView):
    list_display = ("name", "price")
    search_fields = ("name",)


class ProductPages(ModelViewSet[Product]):
    resource = Products
    slug = "products"
    list_view = ProductList


class CatalogConfig(AppConfig):
    name = "catalog"
    views = [ProductPages]
```

## Attributes

| Attribute | Default | Description |
| --- | --- | --- |
| `resource` | required | `Resource` class whose query and write policy is shared by these routes. |
| `slug` | `None` | URL suffix used for the registered model routes. |
| `prefix` | `None` | Alternate URL suffix. If both `prefix` and `slug` are set, `prefix` takes precedence. |
| `list_view` | `None` | `ListView` subclass for the collection page. |
| `detail_view` | `None` | `DetailView` subclass for the record page. |
| `create_view` | `None` | `CreateView` subclass for the create page. |
| `update_view` | `None` | `UpdateView` subclass for the edit page. |
| `delete_view` | `None` | `DeleteView` subclass for the delete page. |

If a view class is omitted, Obox uses the corresponding built-in view. The route
prefix is combined with the configured UI prefix and application view prefix.
Override `view_prefix` on `AppConfig` to change the application-level prefix.

Use `Obox.register(resource, list_view=..., detail_view=..., create_view=...,
update_view=..., delete_view=...)` when declarative app registration is not
needed. Setting `views=False` disables generated HTML routes for that
registration.
