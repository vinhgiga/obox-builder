import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from obox import forms
from pydantic import BaseModel, ConfigDict, Field

REFERENCE = os.environ.get("OBOX_REFERENCE_PYTHON")


@pytest.mark.skipif(
    not REFERENCE, reason="Set OBOX_REFERENCE_PYTHON to the isolated reference Python"
)
def test_supported_validation_contract_matches_django() -> None:
    class ContactInput(BaseModel):
        model_config = ConfigDict(str_strip_whitespace=True)
        name: str = Field(min_length=1, max_length=5)
        age: int | None = None

    class ContactForm(forms.Form[ContactInput]):
        schema = ContactInput

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
            results.append(
                {
                    "valid": valid,
                    "fields": sorted(obj.errors),
                    "data": obj.cleaned_data.model_dump()
                    if valid and obj.cleaned_data is not None
                    else None,
                }
            )
        return results

    reference = json.loads(expected.stdout)
    assert asyncio.run(scenario()) == [
        {
            "valid": item["valid"],
            "fields": sorted(item["errors"]),
            "data": item["data"] if item["valid"] else None,
        }
        for item in reference
    ]
