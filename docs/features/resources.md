# Resources and views

Register a mapped model to publish both HTML and REST CRUD.

```python
obox.register(Product)
```

Declare a `Resource` for shared field selection, validation, record visibility
and write policy.

```python
from decimal import Decimal

from obox import Resource
from pydantic import BaseModel

from .models import Product


class ProductInput(BaseModel):
    name: str
    price: Decimal


class ProductResource(Resource[Product]):
    model = Product
    fields = ("id", "name", "price")
    schema = ProductInput
    ordering = ("name",)
```

Customize the generated HTML views with `ListView`, `DetailView`, `CreateView`,
`UpdateView` and `DeleteView` subclasses, then pass them to `register`.

```python
from obox.views.generic import ListView

from .resources import ProductResource


class ProductList(ListView):
    list_display = ("id", "name", "price")
    search_fields = ("name",)
    paginate_by = 25


obox.register(ProductResource, list_view=ProductList)
```

`ListView` options include `list_display`, `list_display_links`, `list_filter`,
`search_fields`, `paginate_by` and `actions`. The edit views own `fields`,
`fieldsets`, `inlines` and `form_class`; `DetailView` displays the resource's
readable fields. Pass a `ModelApi` subclass with `api=...` to replace the REST
adapter. Use `views=False` or `api=False` to publish a single surface.

Register from `AppConfig.ready()` for app-prefixed routes. `AppConfig.views` and
`AppConfig.apis` also accept `Resource` classes, `ModelViewSet`/`ModelApi`
subclasses, or dotted paths for declarative registration.

See the [registration example](../examples/features/register-resource.md),
[list customization](../examples/views/list-and-filters.md) and
[Python reference](../reference/python-api.md).
