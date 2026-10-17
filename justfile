# List available recipes.
default:
    @just --list

# Install dev environment (deps + git hooks).
dev:
    uv sync --all-extras
    uv run pre-commit install

# Run the test suite.
test:
    uv run pytest

# Static checks: ruff (lint + format), pyright, import-linter.
lint:
    uv run ruff check .
    uv run ruff format --check .
    uv run pyright
    uv run lint-imports

# Auto-fix lint/format issues.
fmt:
    uv run ruff check --fix .
    uv run ruff format .

# Fix, then verify everything.
check: fmt lint test

# Re-vendor Tabler + Tabler Icons into contrib/admin/static.
vendor:
    uv run python scripts/vendor_frontend.py

# Serve the documentation site with live reload.
docs:
    uv run --group docs mkdocs serve

# Build the documentation site (strict).
docs-build:
    uv run --group docs mkdocs build --strict
