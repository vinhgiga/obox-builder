# CLI

```sh
obox startproject NAME [DIRECTORY]
obox createsuperuser --settings settings
obox jobs --settings settings worker --once
obox jobs --settings settings enqueue task_name --args '[]' --kwargs '{}'
obox jobs --settings settings status TASK_ID
obox jobs --settings settings retry TASK_ID
obox jobs --settings settings revoke TASK_ID
obox jobs --settings settings purge --days 30
```

`startproject` creates canonical `settings.py` and `urls.py`, with a unique secret.
The generated initial auth migration makes `alembic upgrade head` sufficient
before creating the first administrator. It preserves unrelated files and refuses to overwrite generated targets.
Auth and worker commands use `OBOX_SETTINGS_MODULE`, defaulting to `settings`.
Task modules can be supplied with `--module`; imports register task functions.
The commands do not create schema: run the host's Alembic migrations first.
