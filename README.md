# obox-builder

Internal FastAPI admin framework, "Django-like at the mechanism level": projects and apps, lazy
settings, a `manage.py`-style CLI (`obox`), built-in auth + RBAC, admin UI on Tabler + Tailwind.

- Python: floor 3.11 (CI tests 3.11-3.13)
- Runtime: async-only (SQLAlchemy 2.0 `AsyncSession`), multiple engines supported
  (default `postgresql+psycopg_async`, `asyncpg` and `sqlite+aiosqlite` supported)
- UI: Jinja2 SSR + Tabler, Tailwind v4 with the `tw:` prefix
- CLI: Typer/Click, commands live in each app's `management/commands/`

Architecture decisions: `docs/adr/`.

Docs live in `docs/` and are published at <https://vinhgiga.github.io/obox-builder/>
(start with the [quick start](docs/quickstart.md)); serve them locally with `just docs`.

## Testing

`obox_builder.testing` provides a pytest plugin (auto-registered) with
`db`/`client`/`login` fixtures plus user/role factories. See
[docs/testing.md](docs/testing.md) for the project setup.

## Dev

```shell
uv sync --all-extras
uv run pytest
uv run ruff check .
uv run pyright
uv run lint-imports
docker compose up -d db
```
