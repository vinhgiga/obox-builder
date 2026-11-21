# Register a resource

A model alone exposes HTML and REST CRUD.

```python
obox.register(Product)
```

Use a shared Pydantic schema when both surfaces need the same validation.

```python
from decimal import Decimal

from obox import Resource
from pydantic import BaseModel, field_validator

from .models import Product


class ProductInput(BaseModel):
    name: str
    price: Decimal

    @field_validator("price")
    @classmethod
    def positive_price(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("Enter a positive price.")
        return value


class ProductResource(Resource[Product]):
    model = Product
    fields = ("id", "name", "price")
    schema = ProductInput
    ordering = ("name",)
```

Register it at the root with `obox.register(ProductResource)`, or in
`AppConfig.ready()` for app-prefixed URLs.

```python
from obox.apps import AppConfig


class ProductsConfig(AppConfig):
    name = "app.products"
    label = "catalog"

    def ready(self):
        self.register(ProductResource)
```

The app-prefixed collections are `/catalog/products` and
`/api/catalog/products`. Pass `api=False` or `views=False` to publish a single
surface. Pass `builder=ProductBuilder` to change presentation or select a
specialized `ModelApi`.

Set `fields` to a fixed projection for sensitive models. Override
`get_queryset(actor)` to limit record visibility before lookup and pagination,
`can(action, obj, actor)` for write rules and `get_writable_fields(action, obj, actor)`
to narrow writable fields.
