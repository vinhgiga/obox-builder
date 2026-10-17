from __future__ import annotations

from dataclasses import dataclass

from typer._click import echo as click_echo  # pyright: ignore[reportPrivateImportUsage]
from typer._click.termui import style as click_style  # pyright: ignore[reportPrivateImportUsage]

from obox_builder.core.checks import CheckLevel, CheckMessage


@dataclass
class CliState:
    verbosity: int = 1
    no_color: bool = False


state = CliState()

LEVEL_COLORS = {
    CheckLevel.ERROR: "red",
    CheckLevel.WARNING: "yellow",
    CheckLevel.INFO: "cyan",
    CheckLevel.DEBUG: "dim",
}


def get_verbosity() -> int:
    return state.verbosity


def echo(
    message: str = "",
    *,
    level: int = 1,
    err: bool = False,
    fg: str | None = None,
) -> None:
    """Print ``message`` when the current verbosity is at least ``level``."""
    if state.verbosity < level:
        return
    if fg and not state.no_color:
        message = click_style(message, fg=fg)
    click_echo(message, err=err)


def format_check_message(message: CheckMessage) -> str:
    ident = f" ({message.id})" if message.id else ""
    text = f"{message.level.name}{ident}: {message}"
    if message.hint:
        text += f"\n  HINT: {message.hint}"
    color = LEVEL_COLORS.get(message.level)
    if color and not state.no_color:
        return click_style(text, fg=color)
    return text
