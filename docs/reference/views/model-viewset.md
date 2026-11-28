# `ModelViewSet`

`ModelViewSet` binds one shared `ModelResource` to its HTML routes. Declare a
`ModelViewSet` subclass in an installed app's `views` sequence.

Without a `slug` or `prefix` override, the HTML suffix comes from the model
class name in lowercase kebab case, so `Product` uses `/product`. The REST
suffix defaults to the SQLAlchemy table name.

```python
from obox.apps import AppConfig
from obox.views.generic import GridView, ModelViewSet

from .models import Product
from .resources import Products


class ProductGrid(GridView[Product]):
    fields = ("name", "price")


class ProductPages(ModelViewSet[Product]):
    resource = Products
    slug = "products"
    list_view = ProductGrid


class CatalogConfig(AppConfig):
    name = "catalog"
    views = [ProductPages]
```

Here, `slug = "products"` explicitly chooses the plural suffix. Remove it to
use `/catalog/product` for the default `Product` route.

## Attributes

| Attribute | Default | Description |
| --- | --- | --- |
| `resource` | required | `ModelResource` class whose query and write policy is shared by these routes. |
| `slug` | `None` | URL suffix used for the registered model routes. |
| `prefix` | `None` | Alternate URL suffix. If both `prefix` and `slug` are set, `prefix` takes precedence. |
| `list_view` | `None` | Primary collection view. Defaults to the built-in `GridView`. |
| `detail_view` | `None` | `DetailView` subclass for the record page. |
| `create_view` | `None` | `CreateView` subclass for the create page. |
| `update_view` | `None` | `UpdateView` subclass for the edit page. |
| `delete_view` | `None` | `DeleteView` subclass for the delete page. |

The Grid is at `/<prefix>`. The runtime applies the configured app and UI
prefixes. For direct registration, use
`obox.register(Products, views=ProductPages)`. Set `views=False` to publish
only REST.
