"""Copy every static source file into ``STATIC_ROOT`` (collectstatic)."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from obox_builder.conf import settings
from obox_builder.exceptions import ImproperlyConfigured
from obox_builder.staticfiles.finders import find_static_directories


@dataclass
class CollectResult:
    """Outcome of a collectstatic run."""

    copied: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    dry_run: bool = False
    cleared: bool = False


def collect_static(*, clear: bool = False, dry_run: bool = False) -> CollectResult:
    """Collect static files into ``STATIC_ROOT``; first found wins on conflicts."""
    static_root = settings.STATIC_ROOT
    if static_root is None:
        raise ImproperlyConfigured("STATIC_ROOT is not set; cannot collect static files.")
    root = Path(static_root)

    result = CollectResult(dry_run=dry_run)
    if clear and root.exists():
        result.cleared = True
        if not dry_run:
            shutil.rmtree(root)

    seen: set[str] = set()
    for directory in find_static_directories():
        for source in sorted(directory.rglob("*")):
            if not source.is_file():
                continue
            relative = source.relative_to(directory).as_posix()
            if relative in seen:
                result.duplicates.append(relative)
                continue
            seen.add(relative)
            if not dry_run:
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            result.copied.append(relative)
    return result
