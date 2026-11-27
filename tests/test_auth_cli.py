from __future__ import annotations

import asyncio
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any

import pytest
from obox.contrib.auth import cli as auth_cli
from obox.contrib.auth import passwords as auth_passwords
from obox.contrib.auth.password_validation import (
    MinimumLengthValidator,
    NumericPasswordValidator,
    PasswordValidator,
    validate_password,
)


@pytest.mark.parametrize(
    ("passwords", "answers", "expected_validation_calls"),
    [
        (["1234567", "1234567"], ["y"], [True, False]),
        (["1234567", "1234567", "safe-pass-123", "safe-pass-123"], ["n"], [True, True]),
    ],
)
def test_interactive_superuser_password_validation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    passwords: list[str],
    answers: list[str],
    expected_validation_calls: list[bool],
) -> None:
    password_inputs = iter(passwords)
    bypass_answers = iter(answers)
    password_prompts: list[str] = []
    bypass_prompts: list[str] = []
    validation_calls: list[bool] = []

    def get_password(prompt: str) -> str:
        password_prompts.append(prompt)
        return next(password_inputs)

    def answer_bypass(prompt: str) -> str:
        bypass_prompts.append(prompt)
        return next(bypass_answers)

    async def fake_hash_password(
        password: str,
        *,
        validate: bool = True,
        validators: Sequence[PasswordValidator] = (),
        user: Any = None,
    ) -> str:
        validation_calls.append(validate)
        if validate:
            validate_password(password, validators, user)
        return "encoded-password"

    monkeypatch.setattr("obox.contrib.auth.cli.getpass.getpass", get_password)
    monkeypatch.setattr(auth_cli, "input", answer_bypass, raising=False)
    monkeypatch.setattr(auth_passwords, "hash_password", fake_hash_password)
    validators = (MinimumLengthValidator(), NumericPasswordValidator())
    result = asyncio.run(
        auth_cli._prompt_password_hash(validators, SimpleNamespace(username="admin"))
    )
    assert result == "encoded-password"
    assert validation_calls == expected_validation_calls
    assert password_prompts == ["Password: ", "Password (again): "] * (len(passwords) // 2)
    assert bypass_prompts == ["Bypass password validation and create user anyway? [y/N]: "]
    expected_errors = (
        "Error: This password is too short. It must contain at least 8 characters.\n"
        "Error: This password is entirely numeric.\n"
    )
    assert capsys.readouterr().out == expected_errors * (len(expected_validation_calls) - 1)
