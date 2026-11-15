# Release Notes

This page summarizes the repository's [`CHANGELOG.md`](https://github.com/vinhgiga/obox-builder/blob/main/CHANGELOG.md).
Items under **Unreleased** are development changes and are not a tagged release.

## Unreleased

### Added

- A searchable documentation site with product tabs and example sections for
  common features, views, APIs, forms, pages, and components.
- Plugin composition through `Obox(..., plugins=...)`, dependency checks, and
  optional plugin lifespans.
- Atomic batch writes, filtered API descriptions, JSON sessions, and scoped
  personal API tokens.
- Resource ETags and conditional writes for SQLAlchemy-versioned models.
- Activity history and comments, plus resumable uploads and owned attachments.

### Changed

- Versioned update and delete authorization uses the matching operation policy.
- File API metadata and the attachment widget use camelCase JSON properties.

### Removed

- `Auth.install(obox)`. Install Auth through the constructor's `plugins` option
  or with `obox.install(Auth(...))` before startup.

## 0.2.0 — 2026-10-17

- Added the initial admin CRUD surface, model field configuration, generated
  forms, list search and filters, CSRF protection, and error pages.
- Added `ModelAdmin.base_path` for custom admin routes.

## 0.1.0 — 2026-10-17

- Added project scaffolding, settings, application loading, CLI, database and
  migration helpers, and initial Auth and admin UI features.
- Added the initial test tooling and documentation site.

See the [full changelog](https://github.com/vinhgiga/obox-builder/blob/main/CHANGELOG.md)
for complete entries and future updates.
