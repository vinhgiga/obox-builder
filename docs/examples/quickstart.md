# Start a project

```sh
uv add obox-builder uvicorn
uv run obox startproject example
cd example
uv run alembic upgrade head
```

Create the `app/products/` directory inside `example/`. It can be a Python
namespace package, or you can add empty `__init__.py` files to `app/` and
`app/products/`. Save this SQLAlchemy model as `app/products/models.py`.

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

Add `app.products` to `INSTALLED_APPS` so Obox loads its models. In the generated
`main.py`, import the model and add the registration after `Obox(app)`.

```python
from app.products.models import Product

obox.register(Product)
```

The registration exposes these HTML routes.

| Path | Purpose |
| --- | --- |
| `/products` | Browse products. |
| `/products/add` | Add a product. |
| `/products/{pk}/view` | View a product. |
| `/products/{pk}/change` | Edit a product. |
| `/products/{pk}/delete` | Confirm and submit deletion. |

The REST collection is `/v0/product`. For app-prefixed URLs, move the
registration into `AppConfig.ready()` and use
`self.register(Product)`.

Generate a model migration, review it, apply it and create an administrator.

```sh
uv run alembic revision --autogenerate -m 'add product'
uv run alembic upgrade head
uv run obox createsuperuser
uv run uvicorn main:app --reload
```

For unattended setup, supply `OBOX_SUPERUSER_PASSWORD` in the environment and
run `uv run obox createsuperuser --username admin --noinput`.

Sign in at `/auth/login` and open `/products`. See [ModelResource registration](features/register-resource.md)
to add shared validation and [access control](../features/access-control.md) to assign model permissions.
