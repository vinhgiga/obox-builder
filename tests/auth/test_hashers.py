from __future__ import annotations

from obox_builder.contrib.auth import hashers


def test_hash_and_verify_roundtrip() -> None:
    hashed = hashers.hash_password("s3cure-password")
    assert hashed.startswith("$argon2")
    assert hashers.verify_password("s3cure-password", hashed)
    assert not hashers.verify_password("wrong", hashed)


def test_hashes_are_salted() -> None:
    assert hashers.hash_password("same-password") != hashers.hash_password("same-password")


def test_verify_and_update_returns_new_hash() -> None:
    hashed = hashers.hash_password("s3cure-password")
    valid, updated = hashers.verify_and_update("s3cure-password", hashed)
    assert valid is True
    assert updated is None
    valid, updated = hashers.verify_and_update("wrong", hashed)
    assert valid is False
    assert updated is None


def test_unusable_password_never_verifies() -> None:
    unusable = hashers.make_unusable_password()
    assert not hashers.is_usable_password(unusable)
    assert not hashers.verify_password("anything", unusable)
    assert not hashers.verify_password("anything", "")


def test_unknown_hash_format_does_not_raise() -> None:
    assert not hashers.verify_password("anything", "not-a-hash")
    assert hashers.verify_and_update("anything", "not-a-hash") == (False, None)


def test_dummy_verification_is_silent() -> None:
    assert hashers.verify_dummy("anything") is None
