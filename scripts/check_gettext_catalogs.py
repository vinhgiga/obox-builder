from __future__ import annotations

import difflib
import filecmp
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCALE_DIR = ROOT / "obox_builder" / "locale"
POT_FILE = LOCALE_DIR / "obox.pot"


def run_babel(*arguments: str) -> None:
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is required to check gettext catalogs")

    subprocess.run(  # noqa: S603
        [uv, "run", "pybabel", *arguments],
        check=True,
        cwd=ROOT,
    )


def normalized_pot(path: Path) -> str:
    lines = [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.startswith('"POT-Creation-Date:')
    ]
    return "\n".join(lines).rstrip("\n") + "\n"


def check_extracted_catalog(generated_pot: Path) -> None:
    expected = normalized_pot(POT_FILE)
    actual = normalized_pot(generated_pot)
    if expected == actual:
        return

    diff = difflib.unified_diff(
        expected.splitlines(keepends=True),
        actual.splitlines(keepends=True),
        fromfile=str(POT_FILE),
        tofile=str(generated_pot),
    )
    sys.stderr.write("The checked-in gettext template is out of date:\n")
    sys.stderr.writelines(diff)
    raise SystemExit(1)


def check_compiled_catalogs(temp_locale: Path) -> None:
    shutil.copytree(LOCALE_DIR, temp_locale)
    run_babel("compile", "-d", str(temp_locale), "-D", "obox")

    for po_file in LOCALE_DIR.glob("*/LC_MESSAGES/*.po"):
        checked_in_mo = po_file.with_suffix(".mo")
        generated_mo = temp_locale / po_file.relative_to(LOCALE_DIR)
        generated_mo = generated_mo.with_suffix(".mo")
        if not checked_in_mo.is_file() or not filecmp.cmp(
            checked_in_mo,
            generated_mo,
            shallow=False,
        ):
            sys.stderr.write(f"Compiled catalog is out of date: {checked_in_mo}\n")
            raise SystemExit(1)


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        temp_dir = Path(directory)
        generated_pot = temp_dir / "obox.pot"
        run_babel(
            "extract",
            "-F",
            "babel.cfg",
            "-k",
            "translate:2",
            "-o",
            str(generated_pot),
            "obox_builder",
        )
        check_extracted_catalog(generated_pot)
        run_babel(
            "update",
            "--check",
            "--ignore-pot-creation-date",
            "-i",
            str(generated_pot),
            "-d",
            str(LOCALE_DIR),
            "-D",
            "obox",
        )
        check_compiled_catalogs(temp_dir / "compiled-locale")


if __name__ == "__main__":
    main()
