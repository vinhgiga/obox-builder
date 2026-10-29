from __future__ import annotations

import os
from importlib import import_module
from pathlib import Path

import typer

from obox_builder.conf import SETTINGS_MODULE_ENV
from obox_builder.core.management import utils
from obox_builder.core.management.base import CommandError, command
from obox_builder.core.management.templates import (
    APP_TEMPLATE_DIR,
    ensure_target_available,
    module_path_for,
    render_tree,
    validate_name,
)


@command(help="Create a new app skeleton.", requires_settings=False)
def handle(
    name: str = typer.Argument(..., help="App name (a Python identifier)."),
    directory: str | None = typer.Argument(
        None,
        help="Optional target directory (default: apps/NAME in the project root).",
    ),
) -> None:
    validate_name(name, kind="app")
    if directory:
        target = Path(directory)
        module = module_path_for(directory, fallback=name)
    else:
        target, module = _default_app_target(name)
    ensure_target_available(target)
    context = {
        "app_name": name,
        "app_label": target.name,
        "app_module": module,
        "app_class": "".join(part.capitalize() for part in target.name.split("_")) + "Config",
    }
    created = render_tree(APP_TEMPLATE_DIR, target, context)
    utils.echo(f"Created app {name!r} in {target}.")
    for path in created:
        utils.echo(f"  {path.relative_to(target)}", level=2)
    utils.echo(f"Add {module!r} to INSTALLED_APPS in your settings module.")


def _default_app_target(name: str) -> tuple[Path, str]:
    settings_module_name = os.environ.get(SETTINGS_MODULE_ENV)
    if settings_module_name:
        try:
            module = import_module(settings_module_name)
        except ImportError as exc:
            raise CommandError(
                f"Could not import settings module {settings_module_name!r}: {exc}"
            ) from exc
        settings_file = getattr(module, "__file__", None)
        if not settings_file:
            raise CommandError(f"Settings module {settings_module_name!r} has no filesystem path.")
        project_root = Path(settings_file).resolve().parent.parent
        apps_dir = getattr(module, "APPS_DIR", None)
        if apps_dir is None:
            apps_dir = project_root / "apps"
        apps_dir = Path(apps_dir)
        if not apps_dir.is_absolute():
            apps_dir = project_root / apps_dir
        try:
            app_module_path = apps_dir.resolve().relative_to(project_root).as_posix()
        except ValueError:
            app_module_path = apps_dir.name
        module = module_path_for(str(Path(app_module_path) / name), fallback=f"apps.{name}")
        return apps_dir / name, module

    current_package = Path.cwd()
    if _has_project_settings(current_package):
        return current_package / "apps" / name, f"apps.{name}"

    candidates = [
        path for path in Path.cwd().iterdir() if path.is_dir() and _has_project_settings(path)
    ]
    if len(candidates) == 1:
        package = candidates[0]
        return package / "apps" / name, f"apps.{name}"
    if len(candidates) > 1:
        raise CommandError(
            "More than one project settings package was found; set OBOX_SETTINGS_MODULE "
            "or pass an app directory explicitly."
        )

    if Path("apps").is_dir():
        return Path("apps") / name, f"apps.{name}"
    return Path(name), name


def _has_project_settings(path: Path) -> bool:
    return (path / "config" / "settings.py").is_file() or (path / "settings.py").is_file()
