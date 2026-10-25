from __future__ import annotations

from pathlib import Path

import pytest
from obox_builder.conf import BaseSettings, settings
from obox_builder.core.exceptions import ImproperlyConfigured
from obox_builder.core.storage import FileSystemStorage, default_storage
from pydantic import SecretStr


def configure_storage(**overrides: object) -> None:
    config = BaseSettings(
        SECRET_KEY=SecretStr("storage-tests-secret-key-0123456789abcdef"),
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
    ).model_copy(update=overrides)
    settings.configure(config)


def test_filesystem_storage_saves_private_files_and_preserves_collisions(tmp_path: Path) -> None:
    storage = FileSystemStorage(tmp_path)
    name = "private/avatar.png"
    assert storage.save(name, b"image") == name
    assert storage.exists(name)
    with storage.open(name) as stored:
        assert stored.read() == b"image"
    assert (tmp_path / name).stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "private").stat().st_mode & 0o777 == 0o700

    with pytest.raises(FileExistsError):
        storage.save(name, b"replacement")
    with storage.open(name) as stored:
        assert stored.read() == b"image"
    storage.delete(name)
    assert not storage.exists(name)


@pytest.mark.parametrize("name", ["../secret", "/etc/passwd", "a\\b", "."])
def test_filesystem_storage_rejects_unsafe_names(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError, match="relative paths|storage root"):
        FileSystemStorage(tmp_path).save(name, b"data")


def test_default_storage_uses_a_configured_backend() -> None:
    from tests.support.storage_backend import MemoryStorage

    MemoryStorage.files.clear()
    configure_storage(STORAGE_BACKEND="tests.support.storage_backend.MemoryStorage")
    name = "bucket/object.bin"
    assert default_storage.save(name, b"payload") == name
    assert default_storage.exists(name)
    with default_storage.open(name) as stored:
        assert stored.read() == b"payload"
    default_storage.delete(name)
    assert not default_storage.exists(name)


def test_filesystem_storage_cannot_be_nested_in_static_root(tmp_path: Path) -> None:
    configure_storage(MEDIA_ROOT=tmp_path / "static" / "private", STATIC_ROOT=tmp_path / "static")
    with pytest.raises(ImproperlyConfigured, match="public static directory"):
        FileSystemStorage().save("file.txt", b"data")
