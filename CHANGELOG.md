# Changelog

Follows [Keep a Changelog](https://keepachangelog.com/), versions follow
[SemVer](https://semver.org/).

## [Unreleased]

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
- Admin checks `E101`–`E106`, warnings `W101`/`W102`, and 400/404/409 error
  pages.

## [0.1.0] - 2026-10-17

### Added

- Scaffold: uv + hatchling, ruff, pyright, pytest, import-linter, pre-commit,
  CI on Python 3.11–3.13.
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
