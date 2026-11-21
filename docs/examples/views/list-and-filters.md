# List views and filters

Use a builder for common HTML list options and a resource for record visibility.

```python
from obox import Resource
from obox.builder import ModelBuilder

from .models import Product


class LiveProducts(Resource[Product]):
    model = Product

    def get_queryset(self, actor):
        return super().get_queryset(actor).where(Product.is_live.is_(True))


class ProductBuilder(ModelBuilder):
    list_display = ("name", "price")
    search_fields = ("name",)
    list_per_page = 25


obox.register(LiveProducts, builder=ProductBuilder)
```

The HTML list accepts `?q=lamp`. The same resource visibility applies to the
REST collection and individual records.
