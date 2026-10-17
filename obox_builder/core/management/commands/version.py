from __future__ import annotations

import platform
import sys
from importlib.metadata import PackageNotFoundError, version

from obox_builder import __version__
from obox_builder.core.management import utils
from obox_builder.core.management.base import command

PACKAGES = (
    "fastapi",
    "starlette",
    "pydantic",
    "pydantic-settings",
    "sqlalchemy",
    "alembic",
    "uvicorn",
    "typer",
    "jinja2",
    "pwdlib",
)


def _package_version(name: str) -> str | None:
    try:
        return version(name)
    except PackageNotFoundError:
        return None


@command(help="Version information for obox-builder and its dependencies.", requires_settings=False)
def handle() -> None:
    utils.echo(f"obox-builder {__version__}")
    utils.echo(f"Python {sys.version.split()[0]} ({platform.platform()})")
    for name in PACKAGES:
        found = _package_version(name)
        if found:
            utils.echo(f"{name} {found}")
