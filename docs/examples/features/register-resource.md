# Register a resource

A model alone exposes HTML and REST CRUD.

```python
obox.register(Product)
```

## REST detail response

The default REST prefix is `/v0`. A directly registered model uses its SQLAlchemy
table name for the API route. For example, map Chinook's `album` table and register it:

```python
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Album(Base):
    __tablename__ = "album"

    album_id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str]
    artist_id: Mapped[int]


obox.register(Album)
```

`GET /v0/album/1` returns one detail record with camelCase JSON keys:

```json
{
  "albumId": 1,
  "title": "For Those About To Rock We Salute You",
  "artistId": 1
}
```

Use a shared Pydantic schema when both surfaces need the same validation.

```python
from decimal import Decimal

from obox import ModelResource
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


class ProductResource(ModelResource[Product]):
    model = Product
    fields = ("id", "name", "price")
    write_schema = ProductInput
    search_fields = ("name",)
    ordering_fields = ("name", "price")
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
`/v0/catalog/products`. Pass `api=False` or `views=False` to publish a single
surface.

Customize the generated HTML with view subclasses and the REST adapter with a
`ModelApi` subclass.

```python
from obox.api import ModelApi
from obox.views.generic import CreateView, GridView, ModelViewSet


class ProductGrid(GridView[Product]):
    fields = ("name", "price")


class ProductCreate(CreateView[Product]):
    fieldsets = (("Details", {"fields": ("name", "price")}),)


class ProductApi(ModelApi[Product]):
    resource = ProductResource


class ProductPages(ModelViewSet[Product]):
    resource = ProductResource
    list_view = ProductGrid
    create_view = ProductCreate


class ProductsConfig(AppConfig):
    views = [ProductPages]
    apis = [ProductApi]
```

For direct registration, pass `views=ProductPages` and `api=ProductApi`.

Set `fields` to a fixed projection for sensitive models. Override
`get_queryset(self)` to limit record visibility using `self.principal` before
lookup and pagination, `can(action, obj, actor)` for write rules and
`get_writable_fields(action, obj, actor)` to narrow writable fields.
`prepare_write(values, obj, actor)` runs after validation and can supply
trusted server values such as an owner ID.
