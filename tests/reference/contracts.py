"""Run only in the isolated Django reference environment."""

import json
import sys

import django
from django.conf import settings

settings.configure(SECRET_KEY="reference", USE_I18N=False, INSTALLED_APPS=[])  # noqa: S106
django.setup()

from django import forms  # noqa: E402


class ContactForm(forms.Form):
    name = forms.CharField(max_length=5)
    age = forms.IntegerField(required=False)


results = []
for data in json.load(sys.stdin):
    item = ContactForm(data=data)
    valid = item.is_valid()
    results.append({"valid": valid, "errors": dict(item.errors), "data": item.cleaned_data})
print(json.dumps(results, default=str))
