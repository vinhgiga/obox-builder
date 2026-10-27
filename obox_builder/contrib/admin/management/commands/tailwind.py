"""Compile Tailwind CSS for the admin and project templates.

The input CSS is generated on the fly: it imports the Tailwind theme and
utilities layers with the ``tw:`` prefix (no preflight, see ADR 0004) and
declares an ``@source`` for every installed app's ``templates/`` directory plus
``TEMPLATE_DIRS``.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

import typer

from obox_builder.core.management import utils
from obox_builder.core.management.base import CommandError, command

DEFAULT_OUTPUT = Path("static") / "css" / "tw.css"

BASE_CSS = """\
@layer theme, base, components, utilities;
@import "tailwindcss/theme.css" layer(theme) prefix(tw);
@import "tailwindcss/utilities.css" layer(utilities) prefix(tw);
"""


def _find_spec(name: str) -> object:
    return importlib.util.find_spec(name)


def template_directories() -> list[Path]:
    """Every template directory visible to the engine: apps first, then TEMPLATE_DIRS."""
    from obox_builder.apps import apps
    from obox_builder.conf import settings

    directories: list[Path] = []
    for config in apps.get_app_configs():
        candidate = config.path / "templates"
        if candidate.is_dir():
            directories.append(candidate)
    directories.extend(Path(directory) for directory in settings.TEMPLATE_DIRS)
    return directories


def build_input_css(directories: list[Path]) -> str:
    lines = [BASE_CSS.rstrip("\n")]
    for directory in directories:
        lines.append(f'@source "{directory}";')
    return "\n".join(lines) + "\n"


@command(
    help="Build (or watch) the Tailwind CSS used by admin templates.",
    requires_settings=True,
)
def handle(
    action: str = typer.Argument("build", help="Either 'build' or 'watch'."),
    output: Path = typer.Option(
        DEFAULT_OUTPUT, "--output", "-o", help="Where to write the compiled CSS."
    ),
    input_file: Path | None = typer.Option(
        None, "--input", "-i", help="Custom input CSS (defaults to a generated file)."
    ),
    minify: bool = typer.Option(False, "--minify", help="Minify the output (build only)."),
) -> None:
    if action not in {"build", "watch"}:
        raise CommandError(f"Unknown action {action!r}; expected 'build' or 'watch'.")
    if sys.version_info < (3, 11):
        raise CommandError("The tailwind extra requires Python 3.11 or later.")
    if _find_spec("pytailwindcss") is None:
        raise CommandError(
            "The tailwind command requires the optional dependency: "
            "install it with `pip install 'obox-builder[tailwind]'` "
            "(or `uv add --optional tailwind 'obox-builder[tailwind]'`)."
        )
    import pytailwindcss

    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="obox-tailwind-") as workdir:
        if input_file is None:
            generated = Path(workdir) / "input.css"
            directories = template_directories()
            generated.write_text(build_input_css(directories), encoding="utf-8")
            source = generated
            utils.echo(f"Scanning {len(directories)} template director(ies).")
        else:
            source = input_file.resolve()

        args = ["-i", str(source), "-o", str(output)]
        if action == "watch":
            args.append("--watch")
        elif minify:
            args.append("--minify")

        utils.echo(f"Running tailwindcss ({action}) -> {output}")
        try:
            pytailwindcss.run(
                args,
                cwd=Path.cwd(),
                live_output=action == "watch",
            )
        except Exception as exc:
            raise CommandError(f"Tailwind build failed: {exc}") from exc

    utils.echo(f"Wrote {output}")
