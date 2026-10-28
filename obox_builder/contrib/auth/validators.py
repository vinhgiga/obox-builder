"""Password strength validation."""

from __future__ import annotations

import re
from collections.abc import Sequence
from difflib import SequenceMatcher
from types import SimpleNamespace
from typing import Any, Protocol, cast

from obox_builder.conf import settings
from obox_builder.core.exceptions import ImproperlyConfigured
from obox_builder.utils.module_loading import import_string


class PasswordValidationError(ValueError):
    """Raised when a password fails validation; ``errors`` lists the reasons."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__(" ".join(errors))
        self.errors = errors


class PasswordValidator(Protocol):
    def validate(self, password: str, user: object | None = None) -> None: ...

    def get_help_text(self) -> str: ...


class MinimumLengthValidator:
    def __init__(self, min_length: int = 8) -> None:
        self.min_length = min_length

    def validate(self, password: str, user: object | None = None) -> None:
        if len(password) < self.min_length:
            raise PasswordValidationError(
                [
                    "This password is too short. "
                    f"It must contain at least {self.min_length} characters."
                ]
            )

    def get_help_text(self) -> str:
        return f"Your password must contain at least {self.min_length} characters."


class UserAttributeSimilarityValidator:
    def __init__(
        self,
        user_attributes: Sequence[str] = (
            "username",
            "first_name",
            "last_name",
            "email",
            "full_name",
        ),
        max_similarity: float = 0.7,
    ) -> None:
        if max_similarity < 0.1:
            raise ValueError("max_similarity must be at least 0.1")
        self.user_attributes = user_attributes
        self.max_similarity = max_similarity

    def validate(self, password: str, user: object | None = None) -> None:
        password = password.lower()
        for attribute in self.user_attributes:
            value = getattr(user, attribute, None)
            if not isinstance(value, str) or not value:
                continue
            for part in [*re.split(r"\W+", value.lower()), value.lower()]:
                if len(password) >= 10 * len(part) and len(part) < self.max_similarity / 2 * len(
                    password
                ):
                    continue
                if SequenceMatcher(a=password, b=part).quick_ratio() >= self.max_similarity:
                    raise PasswordValidationError(
                        [f"The password is too similar to the {attribute.replace('_', ' ')}."]
                    )

    def get_help_text(self) -> str:
        return "Your password can't be too similar to your other personal information."


class NumericPasswordValidator:
    def validate(self, password: str, user: object | None = None) -> None:
        if password.isdigit():
            raise PasswordValidationError(["This password is entirely numeric."])

    def get_help_text(self) -> str:
        return "Your password can't be entirely numeric."


def get_password_validators(validator_config: list[dict[str, Any]]) -> list[PasswordValidator]:
    validators = []
    for config in validator_config:
        try:
            validator = import_string(config["NAME"])(**config.get("OPTIONS", {}))
        except (AttributeError, KeyError, TypeError, ValueError, ImproperlyConfigured) as exc:
            raise ImproperlyConfigured(
                f"Cannot load AUTH_PASSWORD_VALIDATORS entry {config.get('NAME')!r}: {exc}"
            ) from exc
        if not callable(getattr(validator, "validate", None)):
            raise ImproperlyConfigured(
                f"AUTH_PASSWORD_VALIDATORS entry {config['NAME']!r} must define validate()."
            )
        validators.append(cast(PasswordValidator, validator))
    return validators


def validate_password(
    password: str,
    user: object | None = None,
    *,
    username: str = "",
    min_length: int | None = None,
) -> list[str]:
    """Return a list of problems with ``password``; empty means acceptable."""
    errors: list[str] = []
    if not password:
        return ["Password must not be empty."]
    if settings.AUTH_PASSWORD_VALIDATORS is not None:
        if user is None and username:
            user = SimpleNamespace(username=username)
        for validator in get_password_validators(settings.AUTH_PASSWORD_VALIDATORS):
            try:
                validator.validate(password, user)
            except PasswordValidationError as exc:
                errors.extend(exc.errors)
        return errors
    if user is not None:
        username = getattr(user, "username", username)
    if min_length is None:
        min_length = int(settings.PASSWORD_MIN_LENGTH)
    if len(password) < min_length:
        errors.append(
            f"This password is too short. It must contain at least {min_length} characters."
        )
    if password.isdigit():
        errors.append("This password is entirely numeric.")
    if username and password.lower() == username.lower():
        errors.append("Password must not be the same as the username.")
    if username and username.lower() in password.lower() and len(username) >= 4:
        errors.append("Password must not contain the username.")
    return errors
