# Resources and builders

Register a mapped model to publish both HTML and REST CRUD.

```python
obox.register(Product)
```

Declare a `Resource` for shared field selection, validation, record visibility
and write policy. Use a `ModelBuilder` for presentation and endpoint options.

```python
from obox.builder import ModelBuilder

from .forms import ProductForm
from .resources import ProductResource


class ProductBuilder(ModelBuilder):
    form = ProductForm
    list_display = ("id", "name", "price")
    search_fields = ("name",)
    list_per_page = 25


obox.register(ProductResource, builder=ProductBuilder)
```

See the [registration example](../examples/features/register-resource.md) and
[Python reference](../reference/python-api.md).
