# Obox v0.1

Less code is better. Obox provides concise patterns for server-rendered CRUD and
REST APIs using FastAPI, asynchronous SQLAlchemy, Pydantic and Jinja2. Install
`obox-builder`; import `obox`. The target version is **0.1.0**.

Register a SQLAlchemy model once to publish authorized HTML and REST CRUD.

```sh
uv add obox-builder uvicorn
uv run obox startproject example
cd example
uv run alembic upgrade head
uv run obox createsuperuser
uv run uvicorn main:app --reload
```

```python
import os

from fastapi import FastAPI
from obox import Obox

from app.products.models import Product

os.environ.setdefault("OBOX_SETTINGS_MODULE", "settings")
app = FastAPI(openapi_url=None)
obox = Obox(app)
obox.register(Product)  # HTML: /product; REST: /v0/product
```

By default, HTML uses the model class name and REST uses the SQLAlchemy table
name. For `Product` mapped to `product`, those paths are `/product` and
`/v0/product`.

Use `views=False` for REST only or `api=False` for HTML only. Register from
`AppConfig.ready()` for app-prefixed routes. Declare a `ModelResource` to share
field selection, validation and access rules between both surfaces. Customize
generated HTML with `ListView`, `DetailView`, `CreateView`, `UpdateView` and
`DeleteView` subclasses, and REST with a `ModelApi` subclass.

Native FastAPI routes declare a permission with `Depends(permission_required(...))`.
Administrators assign permissions through roles. Keep the ASGI lifespan enabled
so Obox validates route registration before serving requests.

See the [quickstart](docs/examples/quickstart.md), [registration examples](docs/examples/features/register-resource.md)
and [Python API](docs/reference/python-api.md).

For development:

```sh
uv sync --all-extras --group docs
pnpm install --frozen-lockfile
uv run mypy obox
uv run ruff check obox tests
uv run pyright
uv run lint-imports
uv run --group test-analytics pytest
pnpm lint
uv run --group docs zensical build --clean
```

PostgreSQL integration tests use `OBOX_TEST_DATABASE_URL`; the demo database is
never a test database.
The `test-analytics` group enables the optional pandas and Polars pivot cases.
