default:
    @just --list

dev:
    uv sync --all-extras
    pnpm install --frozen-lockfile
    uv run pre-commit install

lint:
    uv run ruff check obox_builder
    uv run ruff format --check obox_builder
    uv run mypy obox_builder
    uv run pyright
    uv run lint-imports
    pnpm lint

fmt:
    uv run ruff check --fix obox_builder
    uv run ruff format obox_builder

# Extract messages and refresh locale catalogs.
messages:
    uv run pybabel extract -F babel.cfg -k translate:2 -o obox_builder/locale/obox.pot obox_builder
    uv run pybabel update -i obox_builder/locale/obox.pot -d obox_builder/locale -D obox

# Compile gettext catalogs for runtime use.
compile-messages:
    uv run pybabel compile -d obox_builder/locale -D obox

check: fmt lint

build:
    uv build
