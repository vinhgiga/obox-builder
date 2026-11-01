default:
    @just --list

dev:
    uv sync --all-extras
    pnpm install --frozen-lockfile
    uv run pre-commit install

lint:
    uv run pre-commit run --all-files
    uv run pre-commit run actionlint --hook-stage manual --all-files
    pnpm lint
    pnpm test:commitlint

fmt:
    uv run ruff check --fix .
    uv run ruff format .
    pnpm exec biome format --write .

test:
    uv run pytest -q

ci:
    just lint
    just test

# Extract messages and refresh locale catalogs.
messages:
    uv run pybabel extract -F babel.cfg -k translate:2 -o obox_builder/locale/obox.pot obox_builder
    uv run pybabel update -i obox_builder/locale/obox.pot -d obox_builder/locale -D obox

# Compile gettext catalogs for runtime use.
compile-messages:
    uv run pybabel compile -d obox_builder/locale -D obox

check: ci

build:
    pnpm build:css
    uv run validate-pyproject pyproject.toml
    uv build
    uv run twine check dist/*
