# CLI

```sh
obox startproject NAME [DIRECTORY]
obox collectstatic DIRECTORY
obox createsuperuser [--settings settings] [--username NAME] [--noinput]
obox jobs [--settings settings] [--module module] worker [--queue QUEUE] [--concurrency N] [--once]
obox jobs [--settings settings] [--module module] enqueue NAME [--args JSON] [--kwargs JSON]
obox jobs [--settings settings] status ID
obox jobs [--settings settings] retry ID
obox jobs [--settings settings] revoke ID
obox jobs [--settings settings] purge [--days N]
obox webhooks worker --app module:attribute [--factory] [--once]
obox webhooks prune --app module:attribute [--factory]
```

`startproject` creates `settings.py`, `main.py`, `models.py`, `.editorconfig`,
`justfile`, `alembic.ini` and the `migrations/` package, with a unique secret.
The generated initial auth migration makes `alembic upgrade head` sufficient
before creating the first administrator. It preserves unrelated files and
refuses to overwrite generated targets.

`collectstatic` copies the packaged assets into the given directory and prints
the asset manifest as JSON.

Auth and job commands use `OBOX_SETTINGS_MODULE`, defaulting to `settings`.
Task modules can be supplied with `--module`; imports register task functions.
Webhook commands load the host application from `--app` and require the
Webhooks feature to be installed. The commands do not create schema: run the
host's Alembic migrations first.
