# Changelog

Follows [Keep a Changelog](https://keepachangelog.com/), versions follow
[SemVer](https://semver.org/).

## [Unreleased]

### Added

- M0: repo scaffold (uv, hatchling, ruff, pyright, pytest, import-linter, pre-commit, CI 3.11-3.13).
- M1: `conf` (lazy settings, `OBOX_SETTINGS_MODULE`), 3-phase `apps` registry, system checks,
  `setup()`.
- M2: management CLI (`obox` script and `manage.py`): lazy command discovery with app override
  precedence and typo suggestions, `@command` decorator, common options
  (`--settings`, `--pythonpath`, `-v/--verbosity`, `--traceback`, `--no-color`, `--skip-checks`),
  core commands `startproject`, `startapp`, `runserver`, `check`, `version`,
  project/app scaffolds with a random `SECRET_KEY`, and `create_app()`/`get_asgi_application()`
  wiring `MIDDLEWARE` and `ROOT_URLCONF` routers.
- M3: `db` layer (`Model` with `<app_label>_<snake_name>` table names and registry-derived
  app labels, `TimestampMixin`, `relationship()` defaulting to `lazy="raise"`, JSONB/TZDateTime
  types, per-URL async engine cache with `run_async()` bootstrap, `session_scope()`/`get_db()`
  session helpers) and Alembic integration (`makemigrations`, `migrate [--sql]`,
  `showmigrations`, `shell`, `dbshell`; in-code config derived from the settings module path;
  autogeneration filtered to installed apps; `post_migrate` signal emitted after `migrate`).
  FastAPI `lifespan` now owns engine creation and disposal.
- M4: `contrib.auth` — `User`/`Role`/`Permission`/`Session` models (DB-backed session auth),
  Argon2id hashing (`pwdlib`) with a dummy-verify user-enumeration guard, password validators,
  auth services (create/set/change password, authenticate with lockout after
  `LOGIN_MAX_ATTEMPTS`), sliding-expiry sessions with hashed tokens and cookie helpers, RBAC
  permissions (`app_label.action` codenames, `__permissions__` extras, `sync_permissions` with
  `--prune`/`--dry-run`, automatic `post_migrate` sync), CSRF helpers, FastAPI dependencies
  (`get_current_user`, `login_required`, `permission_required`), template context processors
  (`PermWrapper`), safe `next` URL validation, and `createsuperuser`/`changepassword`/
  `sync_permissions` commands. Unauthenticated requests get a 303 redirect to `LOGIN_URL`
  (HTML) or 401 JSON; permission denials raise 403 with HTML/JSON negotiation.
- M5: `templating` (Jinja2 environment with `TEMPLATE_DIRS` + per-app template discovery,
  `render()`/`render_template()`, `format_datetime`/`filesizeformat` filters, `request`/`auth`
  context processors), `http` helpers (`redirect`, session-backed flash messages,
  `is_safe_url`/`safe_next_url`), `staticfiles` (`find_static_directories`, `AppStaticFiles`,
  `collect_static`, `static()` URL helper, `collectstatic`/`showurls` commands,
  `STATICFILES_DIRS`), and a security-headers middleware. `contrib.admin`: Tabler 1.6.1 +
  Tabler Icons 3.49.0 vendored in the wheel (no CDN/Node needed), login/logout/password-change
  views with session-cycling login, dashboard with counts, user management (search, pagination,
  create, roles, superuser/last-superuser guards, deactivation revokes sessions), role
  management with a permission matrix grouped by app/model, permission-filtered sidebar
  navigation, `tailwind` command for custom `tw:` classes, and a styled 403 fallback page.
- M6: `obox_builder.testing` — pytest plugin (auto-registered via `pytest11`) with
  `obox_env`/`db`/`client`/`login` fixtures, `build_client()`/`session_cookie()` helpers and
  `make_user()`/`make_role()`/`grant_permissions()` factories; demo project smoke tests
  consume it end-to-end. Docs: `docs/testing.md`.
- Docs: MkDocs Material site (`docs/`) with a quick start, a testing guide, `just docs`
  recipes, a strict docs build in CI and a GitHub Pages deployment workflow.

### Fixed

- `startproject`: the scaffold now depends on `obox-builder[sqlite]`, so the default SQLite
  `DATABASE_URL` works out of the box.
