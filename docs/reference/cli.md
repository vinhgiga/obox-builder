# Command line

Obox Builder installs the `obox` command in the active project environment.
Use `uv run obox --help` and `uv run obox <command> --help` for the options
available in the installed version.

## Project and assets

```shell
uv run obox startproject app
uv run obox collectstatic /var/lib/my-app/static
uv run obox createsuperuser
```

`startproject` creates a runnable scaffold in the target directory.
`collectstatic` publishes the package's immutable UI assets to a persistent
directory. `createsuperuser` creates an Auth administrator using the configured
settings module.

## Background jobs

```shell
uv run obox jobs worker
uv run obox jobs worker --once
uv run obox jobs enqueue refresh_reports --args '[]' --kwargs '{}'
uv run obox jobs status TASK_ID
uv run obox jobs retry TASK_ID
uv run obox jobs revoke TASK_ID
uv run obox jobs purge --days 30
```

Workers import the configured task module by default. Use `--module` to import
additional task modules and `--settings` to choose another settings module.

## Webhook delivery

```shell
uv run obox webhooks worker --app main:app
uv run obox webhooks worker --app main:app --once
uv run obox webhooks prune --app main:app
```

The application must install `Webhooks`. Use `--factory` when the supplied
`module:attribute` is an application factory.
