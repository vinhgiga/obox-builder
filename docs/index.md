# obox-builder

**Django-flavoured internal admin framework built on FastAPI.**

obox-builder brings the batteries of the Django admin to modern, async FastAPI
projects: projects and apps, lazy settings, a `manage.py`-style CLI, database-backed
auth with RBAC, and a Tabler + Tailwind admin UI that ships in the wheel — no Node
or CDN required.

<div class="grid cards" markdown>

- :material-rocket-launch:{ .lg .middle } __Quick start__

    ---

    Scaffold a project, run migrations and log in to the admin in a few minutes.

    [:octicons-arrow-right-24: Quick start](quickstart.md)

- :material-console:{ .lg .middle } __Familiar CLI__

    ---

    `startproject`, `startapp`, `makemigrations`, `migrate`, `runserver` — run
    through `manage.py` or the `obox` command, with per-app command discovery.

- :material-database:{ .lg .middle } __Async by default__

    ---

    SQLAlchemy 2.0 `AsyncSession` throughout, with multiple engines supported:
    PostgreSQL, asyncpg and SQLite.

- :material-shield-lock:{ .lg .middle } __Auth & RBAC included__

    ---

    Users, roles and permissions, database-backed sessions, login lockout, CSRF
    helpers and a permission matrix in the admin.

- :material-tools:{ .lg .middle } __Test helpers__

    ---

    A pytest plugin with `db`, `client` and `login` fixtures, plus user and role
    factories.

    [:octicons-arrow-right-24: Testing](testing.md)

- :material-layers:{ .lg .middle } __App-oriented__

    ---

    Feature-first apps with `AppConfig`, automatic template and static
    discovery, and migrations scoped to installed apps.

</div>

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- PostgreSQL (optional — SQLite works out of the box)

Ready? Head to the [quick start](quickstart.md).
