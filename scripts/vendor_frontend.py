#!/usr/bin/env python
"""Vendor pinned frontend assets (Tabler core + icons) into contrib.admin/static.

Dev-only helper: downloads the npm tarballs listed below, verifies their SHA-256,
and extracts the subset of files the admin UI needs. End users never run this;
the vendored files ship inside the wheel.

Usage: uv run python scripts/vendor_frontend.py [--force]
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VENDOR_ROOT = REPO_ROOT / "obox_builder" / "contrib" / "admin" / "static" / "admin" / "vendor"

PACKAGES: list[dict[str, object]] = [
    {
        "name": "@tabler/core",
        "version": "1.6.1",
        "url": "https://registry.npmjs.org/@tabler/core/-/core-1.6.1.tgz",
        "sha256": "d02d679e51f6c179b21f035e154c13cdcb2ae8b18579e56ea606408ba7fc7a87",
        "files": {
            "package/dist/css/tabler.min.css": "tabler/tabler.min.css",
            "package/dist/js/tabler.min.js": "tabler/tabler.min.js",
        },
    },
    {
        "name": "@tabler/icons-webfont",
        "version": "3.49.0",
        "url": "https://registry.npmjs.org/@tabler/icons-webfont/-/icons-webfont-3.49.0.tgz",
        "sha256": "48c69c65a5f9013d482a17cab8899a09dcaec50750d1ac7324eb63a061fffb82",
        "files": {
            "package/dist/tabler-icons.min.css": "tabler-icons/tabler-icons.min.css",
            "package/dist/fonts/tabler-icons.woff2": "tabler-icons/fonts/tabler-icons.woff2",
            "package/dist/fonts/tabler-icons.woff": "tabler-icons/fonts/tabler-icons.woff",
            "package/LICENSE": "tabler-icons/LICENSE",
        },
    },
]


def download(url: str, target: Path) -> None:
    print(f"  downloading {url}")
    with urllib.request.urlopen(url, timeout=180) as response, open(target, "wb") as file:  # noqa: S310
        shutil.copyfileobj(response, file)


def verify(path: Path, expected_sha256: str) -> None:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != expected_sha256:
        raise SystemExit(
            f"Checksum mismatch for {path.name}:\n  expected {expected_sha256}\n  got      {digest}"
        )


def vendor(force: bool) -> None:
    marker = VENDOR_ROOT / "VERSIONS.txt"
    if marker.exists() and not force:
        print(f"{marker.relative_to(REPO_ROOT)} already exists; pass --force to re-download.")
        return

    versions: list[str] = ["Tabler UI components: MIT License (https://tabler.io)"]
    with tempfile.TemporaryDirectory() as tmp:
        for package in PACKAGES:
            name = str(package["name"])
            version = str(package["version"])
            tarball = Path(tmp) / f"{name.replace('/', '-').replace('@', '')}-{version}.tgz"
            download(str(package["url"]), tarball)
            verify(tarball, str(package["sha256"]))
            with tarfile.open(tarball) as archive:
                for member, destination in dict(package["files"]).items():  # type: ignore[arg-type]
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        raise SystemExit(f"{name}: missing {member} in tarball")
                    target = VENDOR_ROOT / destination
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(extracted.read())
                    print(f"  vendored {destination}")
            versions.append(f"{name}@{version} sha256:{package['sha256']}")

    VENDOR_ROOT.mkdir(parents=True, exist_ok=True)
    marker.write_text("\n".join(versions) + "\n")
    print(f"Wrote {marker.relative_to(REPO_ROOT)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="re-download even when vendored")
    args = parser.parse_args(argv)
    vendor(args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
