"""Build immutable static snapshots without importing application dependencies."""

from __future__ import annotations

import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory

SOURCES = {
    "builder": ("builder/static", "contrib/auth/static"),
    "files": ("contrib/files/static", "builder/static"),
    "analytics": ("contrib/analytics/static", "builder/static"),
}


def fingerprint(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name, content in sorted(files.items()):
        digest.update(name.encode() + b"\0" + hashlib.sha256(content).digest())
    return digest.hexdigest()


def read_assets(directory: Path) -> dict[str, bytes]:
    if not directory.is_dir():
        raise FileNotFoundError(f"Static asset directory does not exist: {directory}")
    assets = {}
    for path in directory.rglob("*"):
        name = path.relative_to(directory)
        if any(part.startswith(".") for part in name.parts) or not path.is_file():
            continue
        if not path.resolve().is_relative_to(directory.resolve()):
            raise ValueError(f"Static asset escapes its directory: {path}")
        assets[name.as_posix()] = path.read_bytes()
    return assets


def publish(assets: dict[str, bytes], directory: Path, digest: str) -> None:
    """Publish atomically and preserve every previously published snapshot."""
    if fingerprint(assets) != digest:
        raise ValueError(f"Static asset fingerprint mismatch: {directory}")
    directory.parent.mkdir(parents=True, exist_ok=True)
    if not directory.exists():
        with TemporaryDirectory(prefix=".obox-", dir=directory.parent) as temporary:
            staging = Path(temporary) / digest
            staging.mkdir()
            for name, content in assets.items():
                path = staging / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            try:
                staging.rename(directory)
            except OSError:
                if not directory.is_dir():
                    raise
    if fingerprint(read_assets(directory)) != digest:
        raise ValueError(f"Published static assets were modified: {directory}")


def collect_assets(package: Path, destination: Path) -> dict[str, str]:
    if any(
        destination.resolve().is_relative_to((package / source).resolve())
        for sources in SOURCES.values()
        for source in sources
    ):
        raise ValueError("Static output must be outside the source asset directories.")
    manifest = {}
    for namespace, sources in SOURCES.items():
        assets: dict[str, bytes] = {}
        for source in sources:
            for name, content in read_assets(package / source).items():
                assets.setdefault(name, content)
        digest = fingerprint(assets)
        publish(assets, destination / namespace / digest, digest)
        manifest[namespace] = digest
    return manifest
