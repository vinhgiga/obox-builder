"""Command-line tools for bootstrapping Obox projects."""

from __future__ import annotations

import argparse
import asyncio
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
    "setting.py": "setting.py.tmpl",
}


def startproject(name: str, directory: Path | None = None) -> Path:
    """Create a runnable project skeleton in the current directory or destination."""
    if not name.isidentifier() or keyword.iskeyword(name):
        raise ValueError("Project name must be a valid Python identifier.")

    destination = (directory or Path(name)).expanduser().resolve()
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise FileExistsError(f"Destination already exists and is not empty: {destination}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    created_directory = not destination.exists()
    if created_directory:
        destination.mkdir()

    created_files: list[Path] = []
    try:
        template_root = files("obox_builder.conf").joinpath("app_template")
        secret_key = secrets.token_urlsafe(48)
        for output_name, template_name in _TEMPLATE_FILES.items():
            content = (
                template_root.joinpath(template_name)
                .read_text(encoding="utf-8")
                .replace("__PROJECT_NAME__", name)
                .replace("__SECRET_KEY__", secret_key)
            )
            output = destination / output_name
            with output.open("x", encoding="utf-8") as stream:
                stream.write(content)
            created_files.append(output)
    except Exception:
        for output in created_files:
            output.unlink(missing_ok=True)
        if created_directory:
            shutil.rmtree(destination)
        raise

    return destination


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="obox")
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("startproject", help="Create a runnable Obox project.")
    start.add_argument("name", help="Python project name")
    start.add_argument("directory", nargs="?", type=Path, help="Output directory")
    from obox_builder.contrib.jobs.cli import add_parser

    add_parser(commands)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "jobs":
            from obox_builder.contrib.jobs.cli import run

            asyncio.run(run(arguments))
            return 0
        destination = startproject(arguments.name, arguments.directory)
    except (ValueError, OSError) as error:
        parser.exit(1, f"obox: error: {error}\n")

    print(f"Created project {arguments.name!r} in {destination}")
    return 0
