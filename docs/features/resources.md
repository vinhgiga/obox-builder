# Resources and views

Register a mapped model to publish both HTML and REST CRUD.

```python
obox.register(Product)
```

Declare a `ModelResource` for shared field selection, validation, record visibility
and write policy.

```python
from decimal import Decimal

from obox import ModelResource
from pydantic import BaseModel

from .models import Product


class ProductInput(BaseModel):
    name: str
    price: Decimal


class ProductResource(ModelResource[Product]):
    model = Product
    fields = ("id", "name", "price")
    write_schema = ProductInput
    search_fields = ("name",)
    ordering = ("name",)
```

Use `ModelViewSet` to attach custom HTML views to a resource in `AppConfig.views`.

```python
from obox.views.generic import GridView, ModelViewSet

from .models import Product
from .resources import ProductResource


class ProductGrid(GridView[Product]):
    fields = ("id", "name", "price")
    page_size = 25


class ProductPages(ModelViewSet[Product]):
    resource = ProductResource
    list_view = ProductGrid
```

Use `obox.register(ProductResource, views=ProductPages)` to select a custom
viewset directly. `GridView` accepts `fields`, `page_size`, `view` and
`template_name`. Edit views accept `fieldsets` and `form_class`. Pass
`api=ProductApi` to customize REST, or set `views=False` or `api=False`
to publish a single surface.

Register from `AppConfig.ready()` for app-prefixed routes. `AppConfig.views` and
`AppConfig.apis` also accept `ModelResource` classes, `ModelViewSet`/`ModelApi`
subclasses, or dotted paths for declarative registration.

See the [registration example](../examples/features/register-resource.md),
[list customization](../examples/views/list-and-filters.md) and
[Python reference](../reference/python-api.md).
