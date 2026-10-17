from __future__ import annotations

import keyword
from collections.abc import Mapping
from pathlib import Path
from string import Template

from obox_builder.core.management.base import CommandError

TEMPLATE_SUFFIX = "-tpl"
OBOX_ROOT = Path(__file__).resolve().parents[2]
PROJECT_TEMPLATE_DIR = OBOX_ROOT / "conf" / "project_template"
APP_TEMPLATE_DIR = OBOX_ROOT / "conf" / "app_template"


def validate_name(name: str, *, kind: str, source: str = "name") -> None:
    if not name.isidentifier() or keyword.iskeyword(name):
        raise CommandError(
            f"{source.capitalize()} {name!r} is not a valid {kind}: it must be a "
            "Python identifier (letters, digits and underscores, not starting "
            "with a digit)."
        )


def ensure_target_available(target: Path) -> None:
    if target.is_file():
        raise CommandError(f"Target {str(target)!r} is a file.")
    if target.is_dir() and any(target.iterdir()):
        raise CommandError(f"Target directory {str(target)!r} already exists and is not empty.")


def render_tree(source: Path, target: Path, context: Mapping[str, str]) -> list[Path]:
    if not source.is_dir():
        raise CommandError(f"Template directory {str(source)!r} does not exist.")
    created: list[Path] = []
    for path in sorted(source.rglob("*")):
        if path.is_dir():
            continue
        relative = path.relative_to(source)
        parts = list(relative.parts)
        if parts[-1].endswith(TEMPLATE_SUFFIX):
            parts[-1] = parts[-1][: -len(TEMPLATE_SUFFIX)]
        destination = target.joinpath(*parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        raw = path.read_text(encoding="utf-8")
        try:
            rendered = Template(raw).substitute(context)
        except ValueError as exc:
            raise CommandError(f"Invalid template {relative}: {exc}") from exc
        destination.write_text(rendered, encoding="utf-8")
        created.append(destination)
    return created


def module_path_for(directory: str, fallback: str) -> str:
    """Turn a relative directory like ``apps/blog`` into ``apps.blog``."""
    parts = Path(directory).parts
    if Path(directory).is_absolute() or any(part == ".." for part in parts):
        return fallback
    for part in parts:
        validate_name(part, kind="module path", source="directory component")
    return ".".join(parts)
