# Quick start

This tutorial walks through a complete obox-builder setup: scaffold a project,
mount the admin, create the database and log in. It takes a few minutes.

## Requirements

- Python 3.11 or later
- [uv](https://docs.astral.sh/uv/)
- Git

## 1. Install obox-builder

!!! note "Pre-release"
    obox-builder 0.1 is not published on PyPI yet, so install it from the
    repository. Once released, `uv add obox-builder` will be enough.

Clone the repository and install its dependencies:

```shell
git clone https://github.com/vinhgiga/obox-builder
cd obox-builder
uv sync --all-extras
```

## 2. Create a project

From the repository root, run the `startproject` command:

```shell
uv run obox startproject demo ../demo
cd ../demo
```

The command generates a Django-style project skeleton:

```text
demo/
├── manage.py           # CLI entry point
├── pyproject.toml      # project metadata and dependencies
├── .env.example        # OBOX_-prefixed environment overrides
├── config/
│   ├── settings.py     # your Settings class
│   ├── urls.py         # ROOT_URLCONF (a list of routers)
│   └── asgi.py         # ASGI application
├── apps/               # your apps live here
├── migrations/         # Alembic migrations
├── static/
└── templates/
```

## 3. Point the project at obox-builder

Open `pyproject.toml` and uncomment the `[tool.uv.sources]` block at the
bottom, so uv resolves obox-builder from your local checkout:

```toml
[tool.uv.sources]
obox-builder = { path = "../obox-builder", editable = true }
```

Then create the project environment:

```shell
uv sync
```

## 4. Mount the admin

`config/urls.py` ships with a `routers` list. Add the admin router to it:

```python
from fastapi import APIRouter

from obox_builder.contrib.admin.site import site as admin_site

router = APIRouter()


@router.get("/")
def index() -> dict[str, str]:
    return {"message": "Hello from demo"}


routers = [router, (admin_site.get_router(), "/admin")]
```

`config/settings.py` already installs `obox_builder.contrib.auth` and
`obox_builder.contrib.admin`, and points `DATABASE_URL` at a local SQLite
file, so the admin works out of the box.

### Language preferences

The scaffold defaults to English and Vietnamese. `LocaleMiddleware` checks a
language URL prefix when routes use `i18n_patterns()`, then the language cookie,
the browser's `Accept-Language` header, and finally `LANGUAGE_CODE`. Set
`LANGUAGES` to control which languages can be selected.

To localize public routes and keep the default language's existing URLs, wrap
their router in `i18n_patterns()`:

```python
from obox_builder.contrib.i18n import i18n_patterns
from obox_builder.http.i18n import router as i18n_router

routers = [
    i18n_patterns(router, prefix_default_language=False),
    i18n_router,
    (admin_site.get_router(), "/admin"),
]
```

This serves the default language at `/` and Vietnamese at `/vi/`. Leaving the
default prefix enabled (the default) serves `/en/` and `/vi/` instead.
With the default prefix disabled, unprefixed routes use `LANGUAGE_CODE`; URL
prefixes still select their own language regardless of cookie or browser
preference.

## 5. Create the database

```shell
uv run manage.py makemigrations
uv run manage.py migrate
```

`makemigrations` generates a migration for the built-in auth models (users,
roles, permissions, sessions). `migrate` applies it and syncs the default
permissions.

## 6. Create a superuser

```shell
uv run manage.py createsuperuser
```

You will be prompted for a username, an email address and a password.

## 7. Run the development server

```shell
uv run manage.py runserver --reload
```

Now open:

- <http://127.0.0.1:8000/> — the project's root view.
- <http://127.0.0.1:8000/admin> — the admin UI. Log in with the superuser you
  just created to reach the dashboard, where you can manage users, roles and
  the permission matrix.

!!! tip "Configuration"
    Settings are pydantic-settings models: every field can be overridden with an
    `OBOX_`-prefixed environment variable or through a `.env` file
    (`OBOX_DATABASE_URL`, `OBOX_SECRET_KEY`, `OBOX_DEBUG`, ...). See
    `.env.example` in the scaffold. To use PostgreSQL, install the
    `obox-builder[postgres]` extra and set a
    `postgresql+psycopg_async://` (or `postgresql+asyncpg://`) `DATABASE_URL`.

## Next steps

**Your first app.** Scaffold an app, register it in `INSTALLED_APPS` and define
models with the SQLAlchemy helpers:

```shell
uv run manage.py startapp blog apps/blog
```

```python
# apps/blog/models.py
import sqlalchemy as sa
from obox_builder.db import Model, TimestampMixin
from sqlalchemy.orm import Mapped, mapped_column


class Post(Model, TimestampMixin):
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(sa.String(200))
    body: Mapped[str] = mapped_column(sa.Text, default="")
```

Add `"apps.blog"` to `INSTALLED_APPS`, then run `makemigrations` and `migrate`
again. The app's CRUD permissions (`blog.view_post`, ...) are synced
automatically after the migration.

**Explore the CLI.** `uv run manage.py --help` lists every command, including
`check`, `shell`, `showurls` and `collectstatic`.

**Write tests.** The [testing guide](testing.md) covers the pytest plugin,
`db`/`client`/`login` fixtures and the user/role factories.
