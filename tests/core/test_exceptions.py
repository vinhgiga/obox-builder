from __future__ import annotations

import pickle

import obox_builder
import pytest
from obox_builder import exceptions
from obox_builder.core import exceptions as legacy


@pytest.mark.parametrize(
    "name", ["OboxError", "ImproperlyConfigured", "NotAuthenticated", "PermissionDenied"]
)
def test_exception_imports_preserve_identity_and_serialization(name: str) -> None:
    error = getattr(exceptions, name)
    assert getattr(legacy, name) is error
    assert issubclass(error, exceptions.OboxError)
    restored = pickle.loads(pickle.dumps(error("example")))  # noqa: S301
    assert type(restored) is error
    assert str(restored) == "example"
    old_payload = pickle.dumps(error("legacy"), protocol=0).replace(
        b"obox_builder.exceptions", b"obox_builder.core.exceptions"
    )
    restored = pickle.loads(old_payload)  # noqa: S301
    assert type(restored) is error
    assert str(restored) == "legacy"


def test_public_configuration_exception_is_unchanged() -> None:
    assert obox_builder.ImproperlyConfigured is exceptions.ImproperlyConfigured
