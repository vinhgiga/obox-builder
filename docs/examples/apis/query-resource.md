# Publish a REST resource

```python
from obox.api import ModelApi

from .models import Product
from .resources import ProductResource


class ProductApi(ModelApi[Product]):
    resource = ProductResource
    search_fields = ("name",)
    ordering_fields = ("name", "price")


obox.register(ProductResource, api=ProductApi, views=False)
```

The generated collection is `/api/products`; detail routes use `/{id}`. The
default actions are list, retrieve, create, patch and delete. Set
`include_router_methods` to select actions and provide an `update_schema` to
enable PUT.

For direct FastAPI integration, attach
`ProductApi().build_router("/api/products")`. Model permissions apply in both
layouts. Add `?search=lamp&order=price.desc` to search and order the results.
Use `limit` and `offset` for paging and `includeTotal=true` to request a count.
