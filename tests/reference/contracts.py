"""Run only in the isolated Django/DRF reference environment."""

import json
import sys

import django
from django.conf import settings

settings.configure(SECRET_KEY="reference", USE_I18N=False, INSTALLED_APPS=[])  # noqa: S106
django.setup()

from django import forms  # noqa: E402

from rest_framework import serializers  # noqa: E402


class ContactForm(forms.Form):
    name = forms.CharField(max_length=5)
    age = forms.IntegerField(required=False)


class ContactSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=5)
    age = serializers.IntegerField(required=False)


payload = json.load(sys.stdin)
cls = ContactForm if payload["kind"] == "form" else ContactSerializer
results = []
for data in payload["inputs"]:
    item = cls(data=data)
    valid = item.is_valid()
    cleaned = (
        getattr(item, "cleaned_data", {}) if payload["kind"] == "form" else item.validated_data
    )
    results.append({"valid": valid, "errors": dict(item.errors), "data": cleaned})
print(json.dumps(results, default=str))
