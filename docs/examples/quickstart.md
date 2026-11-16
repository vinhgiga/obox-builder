# Start a project

```sh
uv add obox-builder uvicorn
uv run obox startproject example
cd example
uv run alembic upgrade head
```

Put a native SQLAlchemy model under `app/products/models.py`, using the scaffold's
Base. For example:

```python
from sqlalchemy import Numeric, String
from sqlalchemy.orm import Mapped, mapped_column
from decimal import Decimal
from models import Base


class Product(Base):
    __tablename__ = "product"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
```

Declare `app/products/apps.py`:

```python
from obox.apps import AppConfig


class ProductsConfig(AppConfig):
    name = "app.products"
```

Add `app.products` to INSTALLED_APPS. In `app/products/admin.py`:

```python
from obox.builder import ModelBuilder, register
from .models import Product


@register(Product)
class ProductBuilder(ModelBuilder):
    fields = ("name", "price")
    list_display = ("id", "name", "price")
    search_fields = ("name",)
```

The generated URLs already mount `path('admin/', site.urls)`. Generate a model
migration, review it, apply it, create an administrator and start the app:

```sh
uv run alembic revision --autogenerate -m 'add product'
uv run alembic upgrade head
uv run obox createsuperuser
uv run uvicorn main:app --reload
```

For JSON endpoints follow the [independent REST example](apis/query-resource.md).
For custom input follow the [ModelForm example](forms/customize-form.md).
