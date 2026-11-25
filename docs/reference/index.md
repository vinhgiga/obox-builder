# Reference

This reference describes the public APIs implemented by Obox. Obox follows a
small Django and Django REST Framework shaped surface while keeping FastAPI
requests, SQLAlchemy models, Pydantic schemas, and asynchronous database work
visible.

## Core APIs

- [Built-in class-based views](views/index.md) — request dispatch, display and
  editing views, and model view registration.
- [Model REST API](api.md) — generated CRUD routes, schemas, query parameters,
  permissions, and extension hooks.
- [Command-line reference](cli.md) — project creation, static assets, user
  administration, jobs, and webhooks.
- [Forms](forms.md) — fields, validation, model forms, formsets, and widgets.
- [Settings](settings.md) — configuration loading, defaults, database and
  template setup, and feature settings.

## Other references

- [Python API](python-api.md) — import paths and registration signatures.
- [Django/DRF mapping](../mapping.md) — supported behavior and native
  differences.
- [REST contract](../api-spec.md) — route, pagination, error, and permission
  contract.
- [Component query filters](../component-query.md)
- [Record and activity API](../record-activity-spec.md)
- [Core design specification](../core-spec.md)
- [Contrib integration contract](../extension-spec.md)

The reference covers the implemented Obox subset. A Django-shaped name does not
imply that every Django or DRF option is available; each page calls out Obox's
behavior and differences.
