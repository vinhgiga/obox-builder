from __future__ import annotations

import os
import secrets
from pathlib import Path

import typer

import obox_builder
from obox_builder import __version__
from obox_builder.core.management import utils
from obox_builder.core.management.base import command
from obox_builder.core.management.templates import (
    PROJECT_TEMPLATE_DIR,
    ensure_target_available,
    render_tree,
    validate_name,
)


@command(help="Create a new obox-builder project skeleton.", requires_settings=False)
def handle(
    name_or_directory: str = typer.Argument(
        ...,
        help="Project directory name, or '.' to create the project in the current directory.",
    ),
    directory: str | None = typer.Argument(
        None,
        help="Optional destination directory for the two-argument form.",
    ),
) -> None:
    if directory is None:
        target = Path(name_or_directory).expanduser()
        name = target.resolve().name
    else:
        name = name_or_directory
        target = Path(directory).expanduser()

    validate_name(name, kind="project")
    ensure_target_available(target, allow_non_empty=target.resolve() == Path.cwd())
    python_requirement, uv_source_config = _uv_project_config(target)
    context = {
        "project_name": name,
        "secret_key": secrets.token_urlsafe(48),
        "obox_version": __version__,
        "python_requirement": python_requirement,
        "uv_source_config": uv_source_config,
    }
    created = render_tree(PROJECT_TEMPLATE_DIR, target, context)
    utils.echo(f"Created project {name!r} in {target}.")
    for path in created:
        utils.echo(f"  {path.relative_to(target)}", level=2)
    utils.echo("Next steps:")
    utils.echo(f"  cd {target}")
    utils.echo("  uv sync")
    utils.echo("  uv run manage.py runserver")


def _uv_project_config(target: Path) -> tuple[str, str]:
    source_root = Path(obox_builder.__file__).resolve().parent.parent
    project_config = source_root / "pyproject.toml"
    is_checkout = project_config.is_file() and 'name = "obox-builder"' in project_config.read_text(
        encoding="utf-8"
    )
    if is_checkout:
        try:
            source_path = Path(os.path.relpath(source_root, target.resolve())).as_posix()
        except ValueError:
            source_path = source_root.as_posix()
        source_config = (
            f'[tool.uv.sources]\nobox-builder = {{ path = "{source_path}", editable = true }}'
        )
        return ">=3.10", source_config
    return ">=3.11", ""
