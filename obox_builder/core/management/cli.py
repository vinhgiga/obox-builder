from __future__ import annotations

import inspect
import sys
from collections.abc import Sequence
from difflib import get_close_matches
from importlib import import_module
from pathlib import Path
from typing import Any, cast

import typer
from typer import _click as click  # pyright: ignore[reportPrivateImportUsage]
from typer.core import TyperGroup
from typer.main import get_command_from_info
from typer.models import CommandInfo

from obox_builder.core.checks import run_checks
from obox_builder.core.exceptions import ImproperlyConfigured
from obox_builder.core.management import utils
from obox_builder.core.management.base import CommandError, CommandSpec
from obox_builder.core.management.loader import (
    CommandLocation,
    get_command_locations,
    read_help,
)
from obox_builder.core.management.options import (
    CommonOptions,
    apply,
    common_params,
    extract_common_options,
)


def _scan_locations() -> dict[str, CommandLocation]:
    from obox_builder.conf import settings

    try:
        installed_apps = list(settings.INSTALLED_APPS)
    except ImproperlyConfigured:
        installed_apps = []
    return get_command_locations(installed_apps)


def _import_command_module(location: CommandLocation) -> Any:
    try:
        return import_module(location.module)
    except Exception as exc:
        raise click.ClickException(
            f"Could not import command module {location.module!r}: {exc}"
        ) from exc


def _make_spec(location: CommandLocation) -> CommandSpec:
    module = _import_command_module(location)
    spec = getattr(module, "handle", None)
    if not isinstance(spec, CommandSpec):
        raise click.ClickException(
            f"Command module {location.module!r} must define a 'handle' callable "
            "decorated with @command."
        )
    return spec


def _run_setup() -> None:
    import obox_builder

    obox_builder.setup()


def _prepare(spec: CommandSpec, options: CommonOptions) -> None:
    if spec.requires_settings:
        _run_setup()
    if spec.requires_checks and not options.skip_checks:
        messages = run_checks()
        for message in messages:
            utils.echo(utils.format_check_message(message), level=0)
        problems = [message for message in messages if message.is_serious]
        if problems:
            raise CommandError(
                f"System check identified {len(problems)} error(s); fix them "
                "or run with --skip-checks."
            )


def _invoke_handle(spec: CommandSpec, inner: Any, kwargs: dict[str, Any]) -> Any:
    result = inner(**kwargs)
    if spec.is_async and inspect.iscoroutine(result):
        from obox_builder.db import run_async

        return run_async(result)
    return result


def _build_command(location: CommandLocation) -> click.Command:
    spec = _make_spec(location)
    info = CommandInfo(
        name=location.name,
        callback=spec.func,
        help=spec.help or None,
        hidden=spec.hidden,
    )
    command = get_command_from_info(info, pretty_exceptions_short=True, rich_markup_mode=None)
    inner = command.callback

    def hook(**kwargs: Any) -> Any:
        options = extract_common_options(kwargs)
        apply(options)
        try:
            _prepare(spec, options)
            return _invoke_handle(spec, inner, kwargs)
        except CommandError as exc:
            if options.traceback:
                raise
            raise click.ClickException(str(exc)) from None
        except ImproperlyConfigured as exc:
            if options.traceback:
                raise
            raise click.ClickException(str(exc)) from None

    command.callback = hook
    command.params.extend(common_params())
    return command


class ManagementGroup(TyperGroup):
    """Lazy ``click.Group``: command modules are imported only to run them."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._locations: dict[str, CommandLocation] | None = None
        self._command_cache: dict[str, click.Command] = {}

    def _get_locations(self) -> dict[str, CommandLocation]:
        if self._locations is None:
            self._locations = _scan_locations()
        return self._locations

    def list_commands(self, ctx: click.Context) -> list[str]:
        return sorted(self._get_locations())

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        cached = self._command_cache.get(cmd_name)
        if cached is not None:
            return cached
        location = self._get_locations().get(cmd_name)
        if location is None:
            return None
        command = _build_command(location)
        self._command_cache[cmd_name] = command
        return command

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        rows = [
            (name, read_help(location.path))
            for name, location in sorted(self._get_locations().items())
        ]
        if rows:
            with formatter.section("Commands"):
                formatter.write_dl(rows)

    def resolve_command(
        self, ctx: click.Context, args: list[str]
    ) -> tuple[str | None, click.Command | None, list[str]]:
        try:
            return super().resolve_command(ctx, args)
        except click.exceptions.UsageError as exc:
            if args and not args[0].startswith("-") and "No such command" in exc.message:
                matches = get_close_matches(args[0], self.list_commands(ctx))
                if matches:
                    suggestions = ", ".join(repr(match) for match in matches)
                    message = exc.message.rstrip(".")
                    exc.message = f"{message}. Did you mean {suggestions}?"
            raise


def create_typer_app() -> typer.Typer:
    typer_app = typer.Typer(
        cls=ManagementGroup,
        add_completion=False,
        no_args_is_help=True,
        rich_markup_mode=None,
    )

    @typer_app.callback()
    def root() -> None:
        """obox-builder management CLI."""

    return typer_app


def create_cli() -> ManagementGroup:
    return cast(ManagementGroup, typer.main.get_command(create_typer_app()))


def execute_from_command_line(argv: Sequence[str] | None = None) -> None:
    args = list(sys.argv if argv is None else argv)
    prog_name = Path(args[0]).name if args else "obox"
    create_cli().main(args=args[1:], prog_name=prog_name)
