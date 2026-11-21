# Publish a REST resource

```python
from obox.api import ModelApi
from obox.builder import ModelBuilder

from .models import Product
from .resources import ProductResource


class ProductApi(ModelApi[Product]):
    resource = ProductResource
    search_fields = ("name",)
    ordering_fields = ("name", "price")


class ProductBuilder(ModelBuilder):
    api_class = ProductApi


obox.register(ProductResource, builder=ProductBuilder, views=False)
```

For direct FastAPI integration, attach `ProductApi().build_router("/api/products")`.
Model permissions apply in both layouts. Add `?search=lamp&order=price.asc`
to search and order the results. Use `limit` and `offset` for paging.
