# Settings

Obox settings are process configuration. Set `OBOX_SETTINGS_MODULE` to a
Python module before accessing `obox.conf.settings`:

```sh
export OBOX_SETTINGS_MODULE=project.settings
```

The settings object loads the module on first access, copies its uppercase
attributes, and fills missing values from Obox defaults. Access without a
settings module raises `ImproperlyConfigured`. Alternatively, call
`settings.configure(**options)` once before accessing settings. Names must be
uppercase; a second call raises `RuntimeError`. Settings are not request-local
state.

## Core settings

| Setting | Default | Runtime effect |
| --- | --- | --- |
| `DEBUG` | `False` | Used as the default for the Auth feature's secure cookie setting. |
| `SECRET_KEY` | `""` | Secret used to configure CSRF signing. An empty value generates a secret for each runtime. Configure the same random value of at least 32 bytes across deployed workers. |
| `API_PREFIX` | `"/v0"` | Default prefix for REST API routes, including the Auth API. |
| `INSTALLED_APPS` | `[]` | App configuration classes loaded during Obox setup. |
| `DATABASES` | `{}` | Database configuration. Obox supports only the `default` alias. |
| `TEMPLATES` | `[]` | Template engine configuration. Obox uses the first entry. |
| `STATIC_ROOT` | `None` | Persistent output directory used for collected assets. |
| `MEDIA_ROOT` | `""` | Default root passed to the Files feature. |
| `LOGIN_REDIRECT_URL` | `None` | Optional destination used by the Auth feature after login. When unset, Auth redirects to its profile route or the site root. |
| `AUTH_PASSWORD_VALIDATORS` | `[]` | Password validator declarations passed to the Auth feature. |
| `TIME_ZONE` | `"America/Chicago"` | Time zone used to interpret naive datetimes when `USE_TZ` is enabled. |
| `USE_TZ` | `True` | Controls time-zone handling for form `DateTimeField` values. |

Obox serves framework assets under `/_static/` relative to the UI prefix.
`STATIC_ROOT` controls the output directory for collected assets.

Obox also declares defaults for `MEDIA_URL` and `LANGUAGE_CODE`. Media routes
and UI language use application configuration. Auth routes, user models, and
backends use the Auth feature configuration.

## Database configuration

Configure a `default` database. When `ENGINE` is omitted, SQLite is used. The
SQLite engine uses `aiosqlite`; the PostgreSQL engine uses `asyncpg`.

```python
DATABASES = {
    "default": {
        "ENGINE": "obox.db.backends.sqlite3",
        "NAME": "db.sqlite3",
    },
}
```

For PostgreSQL, set `ENGINE` to `obox.db.backends.postgresql` and configure
`NAME`, `USER`, `PASSWORD`, `HOST`, and optional `PORT`. `OPTIONS` is passed to
SQLAlchemy as driver connection arguments.

```python
DATABASES = {
    "default": {
        "ENGINE": "obox.db.backends.postgresql",
        "NAME": "app",
        "USER": "app",
        "PASSWORD": "load-from-the-environment",
        "HOST": "localhost",
        "PORT": "5432",
        "OPTIONS": {},
    },
}
```

Obox uses asynchronous SQLAlchemy sessions and transactions. The CLI does not
create database tables; run the application's Alembic migrations before using
database-backed features.

## Templates

Obox reads the first dictionary in `TEMPLATES`. `BACKEND` is an optional dotted
backend class path. The built-in Jinja2 backend accepts:

| Option | Description |
| --- | --- |
| `DIRS` | Template directories searched before application and packaged templates. |
| `APP_DIRS` | Discover templates in installed application template directories. |
| `OPTIONS` | Options passed to the Jinja2 environment. |

```python
TEMPLATES = [
    {
        "BACKEND": "obox.template.backends.jinja2.Jinja2",
        "DIRS": ["templates"],
        "APP_DIRS": True,
        "OPTIONS": {},
    },
]
```

## CSRF cookie settings

These settings control the cookie used by Obox's HTML CSRF protection.

| Setting | Default |
| --- | --- |
| `CSRF_COOKIE_NAME` | `"csrftoken"` |
| `CSRF_COOKIE_AGE` | `31449600` seconds |
| `CSRF_COOKIE_PATH` | `"/"` |
| `CSRF_COOKIE_DOMAIN` | `None` |
| `CSRF_COOKIE_HTTPONLY` | `False` |
| `CSRF_COOKIE_SAMESITE` | `"Lax"` |
| `CSRF_COOKIE_SECURE` | `False` |
| `CSRF_TRUSTED_ORIGINS` | `[]` |

Use secure cookies over HTTPS and add external origins only when the application
needs cross-origin unsafe requests. HTML views check CSRF on unsafe methods by
default; JSON API writes authenticated by cookies require `X-CSRF-Token`.

## Feature settings

Optional feature apps read uppercase dictionaries named for the feature, such
as `AUTH`, `FILES`, `ACTIVITY`, `ANALYTICS`, and `WEBHOOKS`. Their entries are
passed to the corresponding feature constructor. `AUTH.secure` defaults to
`not DEBUG`; `FILES.root` defaults to `MEDIA_ROOT` and can be overridden in the
`FILES` dictionary. See the [feature reference](../features/index.md) for the
options exposed by each feature.

## Generated project settings

`obox startproject` writes a project `settings.py` with values that differ from
the library defaults: `DEBUG` reads `OBOX_DEBUG` and defaults to `True`,
`SECRET_KEY` reads `OBOX_SECRET_KEY` and falls back to a generated secret,
`API_PREFIX` reads `OBOX_API_PREFIX` and defaults to `"/v0"`, SQLite uses
`OBOX_DATABASE_NAME` or a project-local `db.sqlite3`, and the project sets
`TIME_ZONE = "UTC"`. The generated project sets `CSRF_COOKIE_SECURE = not DEBUG`.
`PROJECT_NAME` is generated from the project name and is
used as the FastAPI application title.
