# `ModelViewSet`

`ModelViewSet` binds one shared `ModelResource` to its HTML routes. Declare a
`ModelViewSet` subclass in an installed app's `views` sequence.

```python
from obox.apps import AppConfig
from obox.views.generic import GridView, ListView, ModelViewSet

from .models import Product
from .resources import Products


class ProductGrid(GridView[Product]):
    fields = ("name", "price")
    search_fields = ("name",)


class ProductList(ListView[Product]):
    fields = ("name", "price")


class ProductPages(ModelViewSet[Product]):
    resource = Products
    slug = "products"
    list_view = ProductGrid
    collection_views = (ProductList,)


class CatalogConfig(AppConfig):
    name = "catalog"
    views = [ProductPages]
```

## Attributes

| Attribute | Default | Description |
| --- | --- | --- |
| `resource` | required | `ModelResource` class whose query and write policy is shared by these routes. |
| `slug` | `None` | URL suffix used for the registered model routes. |
| `prefix` | `None` | Alternate URL suffix. If both `prefix` and `slug` are set, `prefix` takes precedence. |
| `list_view` | `None` | Primary collection view. Defaults to the built-in `GridView`. |
| `collection_views` | `()` | Additional `ListView` or `GridView` subclasses with unique slugs. |
| `detail_view` | `None` | `DetailView` subclass for the record page. |
| `create_view` | `None` | `CreateView` subclass for the create page. |
| `update_view` | `None` | `UpdateView` subclass for the edit page. |
| `delete_view` | `None` | `DeleteView` subclass for the delete page. |

The primary collection is at `/<prefix>`. Additional collections are at
`/<prefix>/views/<slug>`. In the example, the slug for the secondary view is
`list`. Both collections link to the resource's shared record routes. The
runtime combines the resource route with the configured UI and application
prefixes, including `AppConfig.view_prefix`.

`Obox.register(resource)` uses the built-in routes. Set `views=False` to
disable HTML routes for a direct registration.
