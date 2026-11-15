# Settings

Obox accepts a `Settings` instance or the import path of a settings module:

```python
from obox_builder import Obox
from obox_builder.contrib.auth import Auth

obox = Obox(app, settings="setting", plugins=(Auth(),))
```

When `settings` is omitted, Obox reads `OBOX_SETTINGS_MODULE`; if that variable
is unset it loads the module named `setting`. Module-level uppercase names map
to `Settings` fields.

## Common settings

| Setting | Purpose | Default |
| --- | --- | --- |
| `DATABASE_URL` | Async SQLAlchemy database URL | Required unless `SESSION_FACTORY` is provided |
| `SESSION_FACTORY` | Host `async_sessionmaker[AsyncSession]` | `None` |
| `PRINCIPAL_DEPENDENCY` | Host identity resolver | `None` |
| `DEFAULT_POLICY` | Default policy for registered resources | `None` |
| `UI_PREFIX` | Admin UI prefix | `/admin` |
| `API_PREFIX` | JSON API prefix | `/api` |
| `API_DEFAULT_LIMIT` | Default API list limit | `None` |
| `API_MAX_LIMIT` | Maximum API list limit | `None` |
| `API_MAX_BATCH` | Maximum records in one batch operation | `None` |
| `INSTALLED_APPS` | Import paths or config objects to load | `()` |
| `SECRET_KEY` | CSRF and signed cursor secret | `None` |

Provide exactly one of `DATABASE_URL` and `SESSION_FACTORY`. `SECRET_KEY`, when
set, must contain at least 32 bytes. The generated scaffold sets a random
development value; replace it through `OBOX_SECRET_KEY` for deployment and keep
it out of source control.

The generated scaffold overrides the API limit defaults with
`API_DEFAULT_LIMIT=100`, `API_MAX_LIMIT=1000`, and `API_MAX_BATCH=100`.

## Generated settings and environment

The `obox startproject` scaffold reads these environment variables:

| Variable | Setting | Default behavior |
| --- | --- | --- |
| `OBOX_SETTINGS_MODULE` | Settings module | `setting` |
| `OBOX_DATABASE_URL` | `DATABASE_URL` | SQLite database in the project directory |
| `OBOX_DEBUG` | Scaffold's `DEBUG` flag | Enabled for local development |
| `OBOX_SECRET_KEY` | `SECRET_KEY` | Generated development secret |

Set `OBOX_DEBUG=false`, a private secret, and a production database URL in the
deployment environment. Configure TLS, host validation, and infrastructure
limits at the host application and deployment layers.

## Explicit Settings object

Applications can bypass module loading and pass an immutable settings instance:

```python
from obox_builder import Settings

settings = Settings(
    database_url="sqlite+aiosqlite:///./app.db",
    ui_prefix="/admin",
    api_prefix="/api",
    api_default_limit=100,
    api_max_limit=1000,
)
```

Use a host-managed `session_factory` instead of `database_url` when the host
owns engine creation and shutdown. See [Python API](python-api.md) for the
configuration types.
