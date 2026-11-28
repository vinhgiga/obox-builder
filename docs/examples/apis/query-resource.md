# Publish a REST resource

Configure query fields on the Resource so Grid and REST share them.

```python
from obox import ModelResource
from obox.api import ModelApi

from .models import Product


class Products(ModelResource[Product]):
    model = Product
    search_fields = ("name",)
    ordering_fields = ("name", "price")


class ProductApi(ModelApi[Product]):
    resource = Products
    actions = ("list", "retrieve")


obox.register(Products, api=ProductApi, views=False)
```

The collection is `/v0/product` for a table named `product`. Append
`?q=lamp&ordering=-price` to search and sort. Use `limit`, `offset` and
`includeTotal=true` to page through the results. The complete
[query contract](../../reference/api.md#collection-parameters-and-response)
includes filters and grouping.
