from __future__ import annotations

import ast
import importlib.util
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

CORE_LABEL = "core"
CORE_COMMANDS_DIR = Path(__file__).resolve().parent / "commands"
COMMANDS_SUBDIR = Path("management") / "commands"


@dataclass(frozen=True)
class CommandLocation:
    name: str
    module: str
    path: Path
    origin: str


def _command_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.glob("*.py") if not path.name.startswith("_"))


def core_commands() -> list[CommandLocation]:
    return [
        CommandLocation(
            name=path.stem,
            module=f"obox_builder.core.management.commands.{path.stem}",
            path=path,
            origin=CORE_LABEL,
        )
        for path in _command_files(CORE_COMMANDS_DIR)
    ]


def app_commands_dir(entry: str) -> Path | None:
    """Return an app's ``management/commands`` directory without importing the app."""
    try:
        spec = importlib.util.find_spec(entry)
    except (ImportError, ValueError):
        return None
    if spec is None or not spec.submodule_search_locations:
        return None
    directory = Path(next(iter(spec.submodule_search_locations))) / COMMANDS_SUBDIR
    return directory if directory.is_dir() else None


def get_command_locations(installed_apps: Sequence[str]) -> dict[str, CommandLocation]:
    """Map command names to their modules.

    Core commands first, then ``INSTALLED_APPS`` in reverse order so an earlier
    app overrides a later app and both override core commands.
    """
    locations: dict[str, CommandLocation] = {}
    for location in core_commands():
        locations[location.name] = location
    for entry in reversed(list(installed_apps)):
        directory = app_commands_dir(entry)
        if directory is None:
            continue
        for path in _command_files(directory):
            locations[path.stem] = CommandLocation(
                name=path.stem,
                module=f"{entry}.management.commands.{path.stem}",
                path=path,
                origin=entry,
            )
    return locations


def read_help(path: Path) -> str:
    """Read a command's help from source without importing the module."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return ""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "handle":
            for decorator in node.decorator_list:
                if isinstance(decorator, ast.Call):
                    for keyword in decorator.keywords:
                        value = keyword.value
                        if (
                            keyword.arg == "help"
                            and isinstance(value, ast.Constant)
                            and isinstance(value.value, str)
                        ):
                            return value.value
            doc = ast.get_docstring(node)
            return doc.splitlines()[0] if doc else ""
    return ""
