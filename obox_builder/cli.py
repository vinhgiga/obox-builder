"""Command-line tools for bootstrapping Obox projects."""

from __future__ import annotations

import argparse
import asyncio
import json
import keyword
import secrets
import shutil
from collections.abc import Sequence
from importlib.resources import files
from pathlib import Path

_TEMPLATE_FILES = {
    ".editorconfig": "editorconfig.tmpl",
    "justfile": "justfile.tmpl",
    "main.py": "main.py.tmpl",
    "models.py": "models.py.tmpl",
    "setting.py": "setting.py.tmpl",
    "alembic.ini": "alembic.ini.tmpl",
    "migrations/env.py": "migrations/env.py.tmpl",
    "migrations/script.py.mako": "migrations/script.py.mako.tmpl",
    "migrations/versions/__init__.py": "migrations/versions/__init__.py.tmpl",
}


def _create_project(name: str, destination: Path, *, allow_existing: bool) -> Path:
    if not name:
        raise ValueError("Project directory must have a name.")

    if destination.exists():
        if not destination.is_dir():
            raise FileExistsError(f"Destination is not a directory: {destination}")
        if not allow_existing and any(destination.iterdir()):
            raise FileExistsError(f"Destination already exists and is not empty: {destination}")
        existing_files = [
            str(destination / filename)
            for filename in _TEMPLATE_FILES
            if (destination / filename).exists()
        ]
        if existing_files:
            raise FileExistsError(f"Project files already exist: {', '.join(existing_files)}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    created_directory = not destination.exists()
    if created_directory:
        destination.mkdir()

    created_files: list[Path] = []
    created_directories: list[Path] = []
    try:
        template_root = files("obox_builder.conf").joinpath("app_template")
        secret_key = secrets.token_urlsafe(48)
        for output_name, template_name in _TEMPLATE_FILES.items():
            content = (
                template_root.joinpath(template_name)
                .read_text(encoding="utf-8")
                .replace("__PROJECT_NAME__", json.dumps(name, ensure_ascii=False))
                .replace("__SECRET_KEY__", secret_key)
            )
            output = destination / output_name
            missing_directories = []
            parent = output.parent
            while parent != destination and not parent.exists():
                missing_directories.append(parent)
                parent = parent.parent
            output.parent.mkdir(parents=True, exist_ok=True)
            created_directories.extend(reversed(missing_directories))
            with output.open("x", encoding="utf-8") as stream:
                stream.write(content)
            created_files.append(output)
    except Exception:
        for output in created_files:
            output.unlink(missing_ok=True)
        for directory in reversed(created_directories):
            directory.rmdir()
        if created_directory:
            shutil.rmtree(destination)
        raise

    return destination


def startproject(name: str, directory: Path | None = None) -> Path:
    """Create a runnable project skeleton using a Python project name."""
    if not name.isidentifier() or keyword.iskeyword(name):
        raise ValueError("Project name must be a valid Python identifier.")

    destination = (directory or Path(name)).expanduser().resolve()
    return _create_project(name, destination, allow_existing=False)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="obox")
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("startproject", help="Create a runnable Obox project.")
    start.add_argument(
        "directory",
        nargs="?",
        type=Path,
        default=Path("."),
        help="Project directory (default: current directory)",
    )
    start.add_argument("legacy_directory", nargs="?", type=Path, help=argparse.SUPPRESS)
    from obox_builder.contrib.auth.cli import add_parser as add_auth_parser
    from obox_builder.contrib.jobs.cli import add_parser

    add_parser(commands)
    add_auth_parser(commands)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "jobs":
            from obox_builder.contrib.jobs.cli import run

            asyncio.run(run(arguments))
            return 0
        if arguments.command == "createsuperuser":
            from obox_builder.contrib.auth.cli import run as run_superuser

            asyncio.run(run_superuser(arguments))
            return 0
        if arguments.legacy_directory is not None:
            project_name = str(arguments.directory)
            destination = startproject(project_name, arguments.legacy_directory)
        else:
            destination = arguments.directory.expanduser().resolve()
            project_name = destination.name
            destination = _create_project(project_name, destination, allow_existing=True)
    except (ValueError, OSError) as error:
        parser.exit(1, f"obox: error: {error}\n")

    print(f"Created project {project_name!r} in {destination}")
    return 0
