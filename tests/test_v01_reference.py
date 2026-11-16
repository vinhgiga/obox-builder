import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from obox import forms
from obox.rest_framework import serializers

REFERENCE = os.environ.get("OBOX_REFERENCE_PYTHON")


@pytest.mark.skipif(
    not REFERENCE, reason="Set OBOX_REFERENCE_PYTHON to the isolated reference Python"
)
@pytest.mark.parametrize("kind", ["form", "serializer"])
def test_supported_validation_contract_matches_django_and_drf(kind: str) -> None:
    class ContactForm(forms.Form):
        name = forms.CharField(max_length=5)
        age = forms.IntegerField(required=False)

    class ContactSerializer(serializers.Serializer):
        name = serializers.CharField(max_length=5)
        age = serializers.IntegerField(required=False)

    inputs = [
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
        input=json.dumps({"kind": kind, "inputs": inputs}),
        text=True,
        capture_output=True,
        check=True,
    )
    cls = ContactForm if kind == "form" else ContactSerializer

    async def scenario() -> list[dict[str, Any]]:
        results = []
        for data in inputs:
            obj = cls(data=data)
            valid = await obj.is_valid()
            values = obj.cleaned_data if isinstance(obj, forms.Form) else obj.validated_data
            results.append({"valid": valid, "errors": obj.errors, "data": values})
        return results

    assert asyncio.run(scenario()) == json.loads(expected.stdout)
