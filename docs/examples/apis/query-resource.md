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

The generated collection is `/v0/product` for `__tablename__ = "product"`.
`/v0/products` is an alias. Detail routes append `/{id}`. The default actions
are list, retrieve, create, update, patch and delete. The generated PUT schema
uses the resource's writable fields. Set `update_schema` to customize it.

For direct FastAPI integration, attach
`ProductApi().build_router("/v0/product")`. Model permissions apply in both
layouts. Add `?search=lamp&order=price.desc` to search and order the results.
Use `limit` and `offset` for paging and `includeTotal=true` to request a count.
