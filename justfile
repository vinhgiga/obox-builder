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

check: fmt lint

build:
    uv build
