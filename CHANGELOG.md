# Changelog

Follows [Keep a Changelog](https://keepachangelog.com/), versions follow
[SemVer](https://semver.org/).

## [Unreleased]

### Added

- HTTP plugin composition through `Obox(app, plugins=[...])` and
  `obox.install(plugin)`, dependency validation, a read-only plugin registry,
  access defaults and optional asynchronous plugin lifespans.
- `obox.register(Model)` as shorthand for an empty `ModelBuilder[Model]` subclass.
- Atomic resource batch writes and principal-filtered API descriptions, sharing
  existing validation, permissions, hooks, and version checks.
- JSON session authentication and opt-in scoped personal tokens with hashed
  storage, explicit expiry, ownership, and revocation. Token scopes also protect
  related reads, activity, and attachment endpoints.
- Resource API optimistic concurrency using strong ETags and required `If-Match`
  headers for models configured with SQLAlchemy `version_id_col`.
- `obox_builder.contrib.activity`: resource revisions and flat comments with
  permission-aware reads, cursor pagination, and conditional comment edits.
- `obox_builder.contrib.files`: resumable uploads, attachment ownership,
  authorized downloads, ordering, and periodic unclaimed-file collection.

### Fixed

- Versioned resource updates and deletes use their own operation permission and
  row scope without requiring retrieve permission.
- File JSON requests, metadata, upload chunk sizes, and the attachment widget
  consistently use camelCase properties. Clients must send `contentType` and
  `ownerId` and read `contentType` and `chunkSize`.
- API specification examples, concurrency rules, list configuration, and planned
  endpoint availability now match the implemented resource API.

### Removed

- `Auth.install(obox)`: use `obox.install(Auth(...))` or constructor plugins.
  Move custom `router=` arguments to `Auth(router=...)`.
- External product comparisons, product-specific commentary and unused vendor icon branding.
- Obsolete framework-specific documentation, generated project READMEs, MkDocs
  dependencies, build commands and documentation deployment workflow. Current
  guides live under `docs/`; package metadata uses `README.md`.

## [0.2.0] - 2026-10-17

### Added

- Admin CRUD: `ModelAdmin` registration (autodiscovery, permissions, sidebar),
  list with search/filters/sorting/pagination, generated create/edit forms
  (readonly, secret and FK fields, validation errors), delete with
  confirmation, CSRF protection and flash messages.
- `ModelAdmin.base_path` to serve a model's CRUD at a custom path under
  `/admin`.
- `fields` (column introspection, `FieldType` registry, `QuerySpec`) and
  `forms` (pydantic schemas, `BoundForm`).
- Admin checks `E101`-`E106`, warnings `W101`/`W102`, and 400/404/409 error
  pages.

## [0.1.0] - 2026-10-17

### Added

- Scaffold: uv + hatchling, ruff, pyright, pytest, import-linter, pre-commit,
  CI on Python 3.11-3.13.
- `conf` lazy settings, `apps` registry, and CLI (`startproject`, `startapp`,
  `runserver`, `check`, `version`).
- `db`: `Model` base, mixins, async engine/session helpers, and Alembic
  integration (`makemigrations`, `migrate`, `showmigrations`, `shell`,
  `dbshell`).
- `contrib.auth`: users/roles/permissions/sessions, Argon2id hashing, RBAC,
  CSRF, auth dependencies, `createsuperuser` and related commands.
- `templating`, `http`, `staticfiles`, a security-headers middleware, and the
  base admin UI (login, dashboard, user/role management, sidebar).
- `obox_builder.testing` pytest plugin and MkDocs Material docs.
