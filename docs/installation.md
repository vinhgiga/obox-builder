# Installation

Python 3.11 or newer is required. Use `uv` for Python dependencies.

```sh
uv add obox-builder uvicorn
# PostgreSQL driver support:
uv add 'obox-builder[postgres]'
```

The distribution version is `0.1.0`. All application imports start with `obox`.
Uvicorn is the host server used by the examples and scaffold. Obox keeps the
server dependency in the host application. See [FastAPI deployment](https://fastapi.tiangolo.com/deployment/manually/).

SQLite's asynchronous driver is included. PostgreSQL uses asyncpg at runtime;
Alembic owns schema migrations. Obox does not create application tables on startup.

```sh
uv run obox startproject example
cd example
uv run alembic upgrade head
uv run obox createsuperuser
uv run uvicorn main:app --reload
```

The scaffold creates `settings.py`, `urls.py`, `main.py`, `models.py` and Alembic
configuration, including an initial migration for its auth tables. Add models under `app/<feature>`, register their AppConfig in
`INSTALLED_APPS`, then generate and review a migration before applying it.

Configure production `SECRET_KEY`, database credentials, `DEBUG = False`, secure
cookies and a persistent file directory through your deployment's settings.
See [settings](reference/settings.md) and [quickstart](examples/quickstart.md).
