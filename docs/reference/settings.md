# Settings

```python
# settings.py
SECRET_KEY = "load-a-long-random-value-from-your-environment"
DEBUG = False
INSTALLED_APPS = ["obox.contrib.auth", "app.products"]
DATABASES = {
    "default": {"ENGINE": "obox.db.backends.sqlite3", "NAME": "db.sqlite3"},
}
TEMPLATES = [
    {
        "BACKEND": "obox.template.backends.jinja2.Jinja2",
        "DIRS": ["templates"],
        "APP_DIRS": True,
        "OPTIONS": {},
    }
]
```

Set `OBOX_SETTINGS_MODULE=settings` before accessing `obox.conf.settings`.
Alternatively call `settings.configure(...)` once. Unconfigured access raises
ImproperlyConfigured; a second configure call raises RuntimeError. Custom settings
must use uppercase names. Settings are process configuration, not per-request state.

PostgreSQL uses `ENGINE = 'obox.db.backends.postgresql'`, plus `NAME`, `USER`,
`PASSWORD`, `HOST`, `PORT`, and optional driver `OPTIONS`. Only the default database
alias is currently supported. Connections and transactions use async SQLAlchemy.

Template OPTIONS are native Jinja2 Environment options; BACKEND may be a dotted
backend class. DIRS precedes application and packaged templates.

Supported shared settings include STATIC_URL/STATIC_ROOT, MEDIA_ROOT/MEDIA_URL,
LANGUAGE_CODE, TIME_ZONE, USE_TZ, LOGIN_URL, LOGIN_REDIRECT_URL,
AUTH_PASSWORD_VALIDATORS, and CSRF_COOKIE_NAME/SECURE/HTTPONLY/SAMESITE and
CSRF_TRUSTED_ORIGINS. See `obox/conf/global_settings.py` for defaults.

Optional contrib apps read settings such as `AUTH`, `FILES`, `ACTIVITY`,
`ANALYTICS` and `WEBHOOKS`. For example, use `AUTH = {"api_tokens": True}` and
`FILES = {"root": MEDIA_ROOT}`. Declare custom routes on FastAPI and register
generated model routes with `Obox.register()` or `AppConfig.register()`.
