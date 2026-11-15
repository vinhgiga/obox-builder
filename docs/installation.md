# Installation

Add Obox Builder to an existing `uv` project:

```shell
uv add obox-builder
```

The package requires Python 3.11 or later and uses FastAPI, SQLAlchemy 2, and
Pydantic 2. Run `uv run obox startproject app` to generate a runnable scaffold,
or install the package into an existing FastAPI application and configure
`Obox` directly.

Follow the [Quickstart](examples/quickstart.md) to add a model and register it
in an `AppConfig`.
