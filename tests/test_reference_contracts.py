import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from obox import forms

REFERENCE = os.environ.get("OBOX_REFERENCE_PYTHON")


@pytest.mark.skipif(
    not REFERENCE, reason="Set OBOX_REFERENCE_PYTHON to the isolated reference Python"
)
def test_supported_validation_contract_matches_django() -> None:
    class ContactForm(forms.Form):
        name: str = forms.field(max_length=5)
        age: int | None = None

    inputs: list[dict[str, Any]] = [
        {},
        {"name": " abc ", "age": "2"},
        {"name": "abcdef", "age": "bad"},
        {"name": "ok", "age": ""},
        {"name": "ok", "age": None},
        {"name": "ok", "extra": "ignored"},
    ]
    source = Path(__file__).parent / "reference" / "contracts.py"
    expected = subprocess.run(  # noqa: S603 - pinned local reference interpreter
        [str(REFERENCE), str(source)],
        input=json.dumps(inputs),
        text=True,
        capture_output=True,
        check=True,
    )

    async def scenario() -> list[dict[str, Any]]:
        results = []
        for data in inputs:
            obj = ContactForm(data=data)
            valid = await obj.is_valid()
            results.append({"valid": valid, "errors": obj.errors, "data": obj.cleaned_data})
        return results

    assert asyncio.run(scenario()) == json.loads(expected.stdout)
