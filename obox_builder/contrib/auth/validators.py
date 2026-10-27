"""Password strength validation."""

from __future__ import annotations

from obox_builder.conf import settings

COMMON_PASSWORDS = frozenset(
    {
        "123456",
        "12345678",
        "123456789",
        "admin",
        "changeme",
        "letmein",
        "password",
        "password1",
        "qwerty",
        "welcome",
    }
)


class PasswordValidationError(ValueError):
    """Raised when a password fails validation; ``errors`` lists the reasons."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__(" ".join(errors))
        self.errors = errors


def validate_password(
    password: str,
    *,
    username: str = "",
    min_length: int | None = None,
) -> list[str]:
    """Return a list of problems with ``password``; empty means acceptable."""
    errors: list[str] = []
    if min_length is None:
        min_length = int(settings.PASSWORD_MIN_LENGTH)
    if not password:
        return ["Password must not be empty."]
    if len(password) < min_length:
        errors.append(f"Password must be at least {min_length} characters long.")
    if password.lower() in COMMON_PASSWORDS:
        errors.append("Password is too common.")
    if username and password.lower() == username.lower():
        errors.append("Password must not be the same as the username.")
    if username and username.lower() in password.lower() and len(username) >= 4:
        errors.append("Password must not contain the username.")
    if len(set(password)) < settings.PASSWORD_MIN_UNIQUE_CHARACTERS:
        errors.append("Password is too simple.")
    return errors
