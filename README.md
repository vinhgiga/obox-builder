# obox-builder

Model-driven forms, views, and REST APIs for FastAPI applications using SQLAlchemy.

## Install

```shell
uv add obox-builder
```

## Documentation

- [Obox Builder documentation](docs/index.md)
- [Quickstart](docs/examples/quickstart.md)
- [Examples](docs/examples/index.md)
- [API reference](docs/reference/api.md)
- [Release notes](docs/release-notes.md)

Preview the documentation site locally with `uv sync --group docs` and
`uv run zensical serve`.

## Requirements

- Python 3.11 or newer
- FastAPI
- SQLAlchemy 2

The SQLite driver and Auth dependencies are installed by default; enable Auth
as a plugin in your application. Optional dependency groups add PostgreSQL
drivers and webhook HTTP delivery support.
