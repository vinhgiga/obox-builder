from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Any

from typer import _click as click  # pyright: ignore[reportPrivateImportUsage]
from typer._click.types import IntRange
from typer.core import TyperOption

from obox_builder.conf import SETTINGS_MODULE_ENV, settings
from obox_builder.core.management import utils

COMMON_OPTION_NAMES = frozenset(
    {"settings", "pythonpath", "verbosity", "traceback", "no_color", "skip_checks"}
)


@dataclass(frozen=True)
class CommonOptions:
    settings: str | None
    pythonpath: str | None
    verbosity: int
    traceback: bool
    no_color: bool
    skip_checks: bool


def common_params() -> list[click.Parameter]:
    return [
        TyperOption(
            param_decls=["--settings"],
            metavar="MODULE",
            help="Dotted path to the settings module (overrides OBOX_SETTINGS_MODULE).",
        ),
        TyperOption(
            param_decls=["--pythonpath"],
            metavar="PATH",
            help="Prepend PATH to sys.path before running the command.",
        ),
        TyperOption(
            param_decls=["-v", "--verbosity"],
            type=IntRange(0, 3),
            default=1,
            show_default=True,
            help="Verbosity level: 0-3.",
        ),
        TyperOption(
            param_decls=["--traceback"],
            is_flag=True,
            help="Print the full traceback when a CommandError is raised.",
        ),
        TyperOption(
            param_decls=["--no-color"],
            is_flag=True,
            help="Disable colored output.",
        ),
        TyperOption(
            param_decls=["--skip-checks"],
            is_flag=True,
            help="Skip system checks.",
        ),
    ]


def extract_common_options(kwargs: dict[str, Any]) -> CommonOptions:
    return CommonOptions(
        settings=kwargs.pop("settings", None),
        pythonpath=kwargs.pop("pythonpath", None),
        verbosity=kwargs.pop("verbosity", 1),
        traceback=kwargs.pop("traceback", False),
        no_color=kwargs.pop("no_color", False),
        skip_checks=kwargs.pop("skip_checks", False),
    )


def apply(options: CommonOptions) -> None:
    if options.pythonpath:
        sys.path.insert(0, options.pythonpath)
    if options.settings:
        os.environ[SETTINGS_MODULE_ENV] = options.settings
        settings.reset()
    utils.state.verbosity = options.verbosity
    utils.state.no_color = options.no_color
