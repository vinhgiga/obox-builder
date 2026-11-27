# Obox v0.1

Obox is a compact web toolkit built on FastAPI and SQLAlchemy. Less code is
better: each public concept has a focused API and explicit behavior.
The installation package is `obox-builder`; Python uses only `obox`.

Use independent forms, generic HTML views, a shared `ModelResource` for CRUD policy,
and `ModelApi` for typed REST endpoints. `Obox(app)` connects them to FastAPI.
SQLAlchemy models and native requests remain visible; database work is asynchronous.

Start with [installation](installation.md) and the [quickstart](examples/quickstart.md).
The [core API contract](core-spec.md) and [REST contract](api-spec.md) describe
the implemented v0.1 subset.

[Contrib features](features/index.md) retain resource editing, analytics, files,
activity, auth and background work. They are optional product functionality.
