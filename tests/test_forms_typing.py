import subprocess
import sys
from pathlib import Path

import pytest

SOURCE = """
from collections.abc import Mapping
from typing import Any, assert_type
from obox.forms import Form, Textarea, field

class Review(Form):
    name: str
    rate: int = field(ge=1, le=5)
    note: str | None = field(Textarea, default=None)
    tags: list[str] = field(default_factory=list)

    def clean_name(self, value: str) -> str:
        return value.upper()

async def validate(raw: Mapping[str, Any]) -> None:
    form = Review(raw)
    if await form.is_valid():
        assert_type(form.name, str)
        assert_type(form.rate, int)
        assert_type(form.note, str | None)
        assert_type(form.tags, list[str])
"""


@pytest.mark.parametrize("checker", ["mypy", "pyright"])
def test_public_api_type_inference_and_invalid_defaults(tmp_path: Path, checker: str) -> None:
    source = tmp_path / "form_types.py"
    command = [sys.executable, "-m", checker]
    if checker == "mypy":
        command += ["--strict", "--no-incremental"]
    source.write_text(SOURCE)
    result = subprocess.run(  # noqa: S603 -- fixed local type checker and test fixture
        [*command, str(source)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    source.write_text(
        SOURCE
        + """
class Invalid(Form):
    bad_default: int = field(default="bad")
    bad_factory: int = field(default_factory=lambda: "bad")
    bad_option: str = field(required=False)

form = Review({})
form.rate = "bad"
"""
    )
    result = subprocess.run(  # noqa: S603 -- fixed local type checker and test fixture
        [*command, str(source)], capture_output=True, text=True, check=False
    )
    assert result.returncode != 0
    assert "4 errors" in result.stdout + result.stderr
