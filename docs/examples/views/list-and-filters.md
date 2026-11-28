# Customize a Grid

Set the Resource's search and sort fields and use a `GridView` subclass for
presentation. A view filter is a display default that the user can clear.

```python
from obox import ModelResource
from obox.db.filters import FilterGroup, FilterTerm
from obox.views.generic import GridView, ModelViewSet, ViewConfig

from .models import Product


class Products(ModelResource[Product]):
    model = Product
    search_fields = ("name",)
    ordering_fields = ("name", "price")


class ProductGrid(GridView[Product]):
    fields = ("name", "price")
    page_size = 25
    view = ViewConfig(
        filter=FilterGroup(children=(FilterTerm("status", "is", "live"),)),
        sort=("-price",),
    )


class ProductPages(ModelViewSet[Product]):
    resource = Products
    list_view = ProductGrid


obox.register(Products, views=ProductPages)
```

Use `?filter=&ordering=name` to clear the default filter and sort by name.
Keep record visibility in `Products.get_queryset()` so the scope applies to
all Grid and REST operations.
