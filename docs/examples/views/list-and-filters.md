# List views and filters

Use a `ListView` subclass for aligned rows and a `ModelResource` for record
visibility. Add the list beside the default Grid through a `ModelViewSet`.

```python
from obox import ModelResource
from obox.apps import AppConfig
from obox.views.generic import ListView, ModelViewSet, ViewConfig
from sqlalchemy import Select

from .models import Product


class LiveProducts(ModelResource[Product]):
    model = Product

    def get_queryset(self) -> Select[tuple[Product]]:
        return super().get_queryset().where(Product.is_live.is_(True))


class ProductList(ListView[Product]):
    fields = ("name", "price")
    search_fields = ("name",)
    page_size = 25
    view = ViewConfig(sort=("name",))


class ProductPages(ModelViewSet[Product]):
    resource = LiveProducts
    collection_views = (ProductList,)


class CatalogConfig(AppConfig):
    views = [ProductPages]
```

The secondary route ends in `/views/list`. Try `?q=lamp`,
`?status__exact=live`, or `?ordering=-price&page=2`. The browser keeps column
visibility and order for each view. The shared resource limits record access on
every route.
