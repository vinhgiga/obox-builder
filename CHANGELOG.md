# Changelog

Follows [Keep a Changelog](https://keepachangelog.com/), versions follow
[SemVer](https://semver.org/).

## [Unreleased]

### Added

- **Row history** (`obox_builder.contrib.history`): append-only
  `history_row_change` written in the same transaction as the data change;
  `Model.__history__` declarations, `history.track()`, secret redaction,
  `history.use()` actor context and `history.bulk_update()`.
- Admin UI: per-row history card and timeline page, global audit feed
  (`/admin/history`) with model/actor/source/op/date filters, change-set
  detail, permission-aware masking, and an "Audit → History" nav entry.
- M2M diffs for declared `relations` (sorted add/remove id sets).
- Append-only DB triggers installed by `migrate`; `history_purge` and
  `history_export` commands (NDJSON/CSV) with per-model retention.
- `HISTORY_*` settings, `pre_record`/`post_record` signals, model checks
  (`E201`–`E203`, `W201`, `W202`), `history.service` read API, and tracking for
  `auth.User`/`auth.Role`.
- Revert/restore: per-entry and per-change-set `POST .../revert` with
  conflict detection (`?force=1`), deleted-row restore, all-or-nothing
  semantics, and `history.restore_row_change`; blocked reverts (M2M, masked,
  redacted, truncated, taken PK) render `admin/revert.html`.
- `VersionedMixin` optimistic locking (`version` column, `StaleDataError` on
  stale flushes) recorded as `row_version` on entries; `W201` warns for
  tracked models without it.
- Masked values in the payload (`__history__["mask"]`), export buttons on the
  audit feed, `GET /admin/history/export` (CSV/NDJSON, `history.export_row_change`)
  and the `history_anonymize` command (actor/object scopes, dry-run).

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
