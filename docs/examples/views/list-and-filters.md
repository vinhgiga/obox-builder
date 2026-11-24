# List views and filters

Use a `ListView` subclass for HTML list options and a `Resource` for record
visibility.

```python
from obox import Resource
from obox.views.generic import ListView

from .models import Product


class LiveProducts(Resource[Product]):
    model = Product

    def get_queryset(self):
        return super().get_queryset().where(Product.is_live.is_(True))


class ProductList(ListView):
    list_display = ("name", "price")
    list_filter = ("status",)
    search_fields = ("name",)
    paginate_by = 25


obox.register(LiveProducts, list_view=ProductList)
```

The HTML list accepts `?q=lamp`, filter parameters derived from `list_filter`
and the `p` page parameter. Use `actions` for bulk actions and
`list_display_links` to choose the linked columns. The same resource visibility
applies to the REST collection and individual records.
