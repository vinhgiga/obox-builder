# Obox v0.1

Less code is better. Obox provides a small subset of Django 5.2 and Django REST
Framework 3.16.1 using FastAPI, asynchronous SQLAlchemy, Pydantic and Jinja2.
Install `obox-builder`; import `obox`. The target version is **0.1.0**.

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
obox.register(Product)  # /products and /api/products
```

Use `views=False` for REST only or `api=False` for HTML only. Register from
`AppConfig.ready()` for app-prefixed routes. Declare a `Resource` to share
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
uv run pytest
pnpm lint
uv run --group docs zensical build --clean
```

Reference tests use a separate environment pinned in `tests/reference/requirements.txt`.
Set `OBOX_REFERENCE_PYTHON` to that environment's Python executable. PostgreSQL
integration tests use `OBOX_TEST_DATABASE_URL`; the demo database is never a test database.
