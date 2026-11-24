# Obox v0.1

Obox is a small Django/DRF-shaped toolkit on FastAPI and SQLAlchemy. Less code is
better: each supported public concept maps to Django 5.2 LTS or DRF 3.16.1.
The installation package is `obox-builder`; Python uses only `obox`.

Use independent forms, generic HTML views, a shared `Resource` for CRUD policy,
and `ModelApi` for typed REST endpoints. `Obox(app)` connects them to FastAPI.
SQLAlchemy models and native requests remain visible; database work is asynchronous.

Start with [installation](installation.md) and the [quickstart](examples/quickstart.md).
The [mapping table](mapping.md) records supported behavior, native differences and
verification. The [core contract](core-spec.md) and [REST contract](api-spec.md)
describe the implemented v0.1 subset.

[Contrib features](features/index.md) retain resource editing, analytics, files,
activity, auth and background work. They are optional product functionality.
