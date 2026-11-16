# Obox v0.1

Less code is better. Obox provides a small subset of Django 5.2 and Django REST
Framework 3.16.1 using FastAPI, asynchronous SQLAlchemy, Pydantic and Jinja2.
Install `obox-builder`; import `obox`. The target version is **0.1.0**.

Forms, generic views, REST endpoints and the admin builder work independently.
`ModelBuilder` coordinates the admin interface; it does not own REST registration.

```sh
uv add obox-builder uvicorn
uv run obox startproject example
cd example
uv run alembic upgrade head
uv run uvicorn main:app --reload
```

Generated projects use `settings.py`, `OBOX_SETTINGS_MODULE`, `DATABASES`,
`TEMPLATES` and `INSTALLED_APPS`. The host application remains native FastAPI:

```python
import os
from fastapi import FastAPI
from obox import Obox

os.environ.setdefault("OBOX_SETTINGS_MODULE", "settings")
app = FastAPI()
Obox(app)
```

Declare a model form and reuse it in a view or builder:

```python
from obox import forms
from obox.builder import ModelBuilder, register
from obox.views.generic import CreateView
from app.products.models import Product


class ProductForm(forms.ModelForm):
    class Meta:
        model = Product
        fields = ("name", "price")


class ProductCreateView(CreateView):
    model = Product
    form_class = ProductForm
    success_url = "/products/"


@register(Product)
class ProductBuilder(ModelBuilder):
    form = ProductForm
    list_display = ("id", "name", "price")
```

Database-aware hooks and validation use `await`. Query hooks return SQLAlchemy
`Select` objects, not executed results or a simulated Django QuerySet.

Optional product features live in `obox.contrib`: resource editing, batch/upsert,
metadata, analytics, activity/comments, files, auth, jobs and webhooks. Their
protocols are separate from the standardized core.

See [installation](docs/installation.md), [quickstart](docs/examples/quickstart.md),
[API mapping](docs/mapping.md), [core contract](docs/core-spec.md),
[REST contract](docs/api-spec.md), and [v0.1 release notes](docs/release-notes.md).

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
