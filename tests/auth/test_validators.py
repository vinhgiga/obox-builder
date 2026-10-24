from __future__ import annotations

# Password validation edges are adapted from Django's auth_tests/test_validators.py.
from obox_builder.contrib.auth.validators import PasswordValidationError, validate_password


def test_valid_password_has_no_errors() -> None:
    assert validate_password("s3cure-password") == []


def test_short_password_is_rejected() -> None:
    errors = validate_password("short", min_length=8)
    assert any("at least 8 characters" in error for error in errors)


def test_common_password_is_rejected() -> None:
    assert validate_password("password", min_length=8) != []


def test_password_equal_to_username_is_rejected() -> None:
    errors = validate_password("alice12345", username="alice12345", min_length=8)
    assert any("username" in error for error in errors)


def test_password_containing_username_is_rejected() -> None:
    errors = validate_password("alice-secret-99", username="alice", min_length=8)
    assert any("contain the username" in error for error in errors)


def test_simple_repeated_password_is_rejected() -> None:
    errors = validate_password("aaaaaaaa", min_length=8)
    assert any("too simple" in error for error in errors)


def test_empty_password_is_rejected() -> None:
    assert validate_password("") == ["Password must not be empty."]


def test_usable_password_may_start_with_unusable_hash_marker() -> None:
    assert validate_password("!aA1bcde", min_length=8) == []


def test_password_validation_error_carries_errors() -> None:
    error = PasswordValidationError(["one", "two"])
    assert error.errors == ["one", "two"]
    assert "one two" in str(error)


def test_min_length_defaults_to_settings(auth_env: None) -> None:
    errors = validate_password("s3cure")
    assert any("at least 8 characters" in error for error in errors)
    assert validate_password("s3cure-password") == []
