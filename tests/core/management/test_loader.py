from __future__ import annotations

from obox_builder.core.management.loader import (
    core_commands,
    get_command_locations,
    read_help,
)

COMMANDS_A = "tests.support.apps.commands_a"
COMMANDS_B = "tests.support.apps.commands_b"


def test_core_commands_include_expected_names() -> None:
    names = {location.name for location in core_commands()}
    assert {"check", "runserver", "startapp", "startproject", "version"} <= names


def test_get_command_locations_scans_app_commands() -> None:
    locations = get_command_locations([COMMANDS_A])
    probe = locations["probe"]
    assert probe.origin == COMMANDS_A
    assert probe.module == f"{COMMANDS_A}.management.commands.probe"


def test_get_command_locations_skips_private_modules() -> None:
    locations = get_command_locations([COMMANDS_A])
    assert "_private" not in locations


def test_earlier_app_wins_for_duplicate_names() -> None:
    locations = get_command_locations([COMMANDS_A, COMMANDS_B])
    assert locations["shared"].origin == COMMANDS_A


def test_app_commands_override_core_commands() -> None:
    locations = get_command_locations([COMMANDS_B])
    assert locations["version"].origin == COMMANDS_B
    assert locations["check"].origin == "core"


def test_get_command_locations_ignores_missing_app() -> None:
    locations = get_command_locations(["tests.support.apps.does_not_exist"])
    assert set(locations) == {location.name for location in core_commands()}


def test_read_help_reads_decorator_help() -> None:
    locations = get_command_locations([COMMANDS_A])
    assert read_help(locations["probe"].path) == "Probe command used by the test suite."
