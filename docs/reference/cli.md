# Command-line reference

The installed `obox` command provides project scaffolding, static asset
collection, user administration, background job management, and webhook
maintenance.

```text
obox [-h] {startproject,collectstatic,createsuperuser,jobs,webhooks} ...
```

Use `obox COMMAND --help` or `obox jobs ACTION --help` to see the options for
your installed version.

## `startproject`

```text
obox startproject NAME [DIRECTORY]
```

Create a project skeleton. `NAME` must be a valid Python identifier. If
`DIRECTORY` is omitted, Obox creates a new directory named `NAME`. If it is
provided, it may be an existing directory; unrelated files are preserved and
generated files are never overwritten.

The command creates:

- `settings.py` and `main.py`
- `models.py`
- `.editorconfig` and `justfile`
- `alembic.ini`
- the `migrations/` package and initial authentication migration

The generated settings use SQLite and a generated secret key. Apply the initial
schema before creating an administrator:

```sh
uv run alembic upgrade head
uv run obox createsuperuser
```

## `collectstatic`

```text
obox collectstatic DIRECTORY
```

Copy Obox's packaged static assets into `DIRECTORY`. The command prints the
asset manifest as JSON. Configure the same persistent directory as
`STATIC_ROOT` when the application serves collected assets.

## `createsuperuser`

```text
obox createsuperuser [--settings MODULE] [--username NAME] [--noinput]
```

The interactive mode prompts for a username and password. Passwords are checked
with the configured `AUTH_PASSWORD_VALIDATORS`.

With `--noinput` (also spelled `--no-input`), supply the username with
`--username` or `OBOX_SUPERUSER_USERNAME`, and the password with
`OBOX_SUPERUSER_PASSWORD`. This mode is intended for controlled provisioning;
keep the password out of command arguments and logs.

`--settings` chooses the settings module. The default is
`OBOX_SETTINGS_MODULE`, or `settings` when the environment variable is unset.
The command uses the configured database and does not create the schema. Apply
the host application's Alembic migrations first.

## `jobs`

```text
obox jobs [--settings MODULE] [--module MODULE] ACTION
```

`--settings` chooses the settings module, defaulting to `OBOX_SETTINGS_MODULE`
or `settings`. `--module` may be repeated to import task modules. If omitted,
Obox imports `<settings-package>.jobs.tasks`, or `tasks` for a top-level
`settings` module, when that module exists. Importing a task module registers
its task functions.

### `jobs worker`

```text
obox jobs [--settings MODULE] [--module MODULE] worker
    [--queue NAME ...] [--concurrency N] [--once]
```

Run a worker. `--queue` may be repeated; the default queue is `default`.
`--concurrency` sets the worker concurrency and defaults to `1`. `--once` checks
for one task, processes it if available, and exits instead of waiting for
shutdown.

### `jobs enqueue`

```text
obox jobs [--settings MODULE] [--module MODULE] enqueue NAME
    [--args JSON] [--kwargs JSON]
```

Enqueue a registered task. `--args` is a JSON array of positional arguments and
defaults to `[]`. `--kwargs` is a JSON object and defaults to `{}`. The command
prints the new task ID.

### `jobs status`, `jobs retry`, and `jobs revoke`

```text
obox jobs [--settings MODULE] [--module MODULE] status ID
obox jobs [--settings MODULE] [--module MODULE] retry ID
obox jobs [--settings MODULE] [--module MODULE] revoke ID
```

`status` prints the task record as JSON. `retry` requests another attempt for a
task, and `revoke` prevents further execution according to the task state; both
print the result of the operation.

### `jobs purge`

```text
obox jobs [--settings MODULE] [--module MODULE] purge [--days N]
```

Delete completed tasks and their attempt records older than `N` days. `N`
defaults to `30` and must be non-negative. The command prints the number of
deleted tasks.

The jobs commands use the configured database and do not run migrations. Apply
the application's Alembic migrations before using the worker or management
commands.

## `webhooks`

```text
obox webhooks {worker,prune} --app MODULE:ATTRIBUTE [--factory] [--once]
```

The host application must install the Webhooks feature. `--app` identifies the
FastAPI app or Obox runtime; `--factory` calls the named attribute before
loading the runtime.

```text
obox webhooks worker --app app.main:app [--factory] [--once]
obox webhooks prune --app app.main:app [--factory]
```

`worker` runs the delivery loop until it receives a shutdown signal. With
`--once`, it performs one pass and exits. `prune` removes eligible historical
delivery records once, then exits. These commands do not create the host
database schema; run its Alembic migrations first.
