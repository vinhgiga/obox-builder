"""Configurable password validators for model-backed auth."""

from __future__ import annotations

import importlib
import re
from collections.abc import Mapping, Sequence
from difflib import SequenceMatcher
from typing import Any, Protocol, cast

from obox_builder.errors import N_, BuilderValidationError, ImproperlyConfigured


class PasswordValidator(Protocol):
    def validate(self, password: str, user: Any = None) -> None: ...


class UserAttributeSimilarityValidator:
    def __init__(
        self,
        user_attributes: Sequence[str] = ("username", "first_name", "last_name", "email"),
        max_similarity: float = 0.7,
    ) -> None:
        if isinstance(user_attributes, str) or any(
            not isinstance(attribute, str) for attribute in user_attributes
        ):
            raise ImproperlyConfigured("user_attributes must be a sequence of strings.")
        if (
            isinstance(max_similarity, bool)
            or not isinstance(max_similarity, int | float)
            or not 0.1 <= max_similarity <= 1
        ):
            raise ImproperlyConfigured("max_similarity must be between 0.1 and 1.")
        self.user_attributes = tuple(user_attributes)
        self.max_similarity = max_similarity

    def validate(self, password: str, user: Any = None) -> None:
        if user is None:
            return
        candidate = password.casefold()
        for attribute in self.user_attributes:
            value = getattr(user, attribute, None)
            if not isinstance(value, str) or not value:
                continue
            normalized = value.casefold()
            parts = [*re.split(r"\W+", normalized), normalized]
            for part in parts:
                if not part:
                    continue
                ratio = SequenceMatcher(a=candidate, b=part).quick_ratio()
                if ratio < self.max_similarity:
                    continue
                raise BuilderValidationError(
                    {"password": [N_("This password is too similar to your personal information.")]}
                )


class MinimumLengthValidator:
    def __init__(self, min_length: int = 8) -> None:
        if isinstance(min_length, bool) or not isinstance(min_length, int) or min_length < 1:
            raise ImproperlyConfigured("min_length must be a positive integer.")
        self.min_length = min_length

    def validate(self, password: str, user: Any = None) -> None:
        if len(password) < self.min_length:
            raise BuilderValidationError(
                {
                    "password": [
                        N_(
                            f"This password is too short. It must contain at least "
                            f"{self.min_length} characters."
                        )
                    ]
                }
            )


class NumericPasswordValidator:
    def validate(self, password: str, user: Any = None) -> None:
        if password.isdigit():
            raise BuilderValidationError({"password": [N_("This password is entirely numeric.")]})


def get_password_validators(
    validator_config: Sequence[Mapping[str, Any]],
) -> tuple[PasswordValidator, ...]:
    validators: list[PasswordValidator] = []
    for config in validator_config:
        if not isinstance(config, Mapping):
            raise ImproperlyConfigured("Each password validator must be a mapping.")
        name = config.get("NAME")
        if not isinstance(name, str):
            raise ImproperlyConfigured("Password validator NAME must be a string.")
        module_name, separator, attribute = name.rpartition(".")
        if not separator:
            raise ImproperlyConfigured(f"Password validator NAME must be a dotted path: {name}.")
        try:
            validator_class = getattr(importlib.import_module(module_name), attribute)
        except (ImportError, AttributeError) as error:
            raise ImproperlyConfigured(f"Could not import password validator {name}.") from error
        if not callable(validator_class):
            raise ImproperlyConfigured(f"Password validator {name} is not callable.")
        options = config.get("OPTIONS", {})
        if not isinstance(options, Mapping):
            raise ImproperlyConfigured(f"OPTIONS for password validator {name} must be a mapping.")
        try:
            validator = validator_class(**options)
        except (TypeError, ValueError) as error:
            raise ImproperlyConfigured(f"Could not configure password validator {name}.") from error
        if not callable(getattr(validator, "validate", None)):
            raise ImproperlyConfigured(f"Password validator {name} has no validate method.")
        validators.append(cast(PasswordValidator, validator))
    return tuple(validators)


def validate_password(
    password: str, validators: Sequence[PasswordValidator], user: Any = None
) -> None:
    errors: list[str] = []
    for validator in validators:
        try:
            validator.validate(password, user)
        except BuilderValidationError as error:
            errors.extend(
                message for field_errors in error.errors.values() for message in field_errors
            )
    if errors:
        raise BuilderValidationError({"password": errors})
