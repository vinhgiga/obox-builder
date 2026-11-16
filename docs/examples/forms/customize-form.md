# Customize a model form

```python
from obox import forms
from obox.core.exceptions import ValidationError
from app.products.models import Product


class ProductForm(forms.ModelForm):
    class Meta:
        model = Product
        fields = ("name", "price")
        labels = {"name": "Product name"}
        widgets = {"name": forms.TextInput(attrs={"placeholder": "Name"})}
        help_texts = {"price": "Enter a positive price."}

    def clean_name(self):
        value = self.cleaned_data["name"]
        if value.lower() == "reserved":
            raise ValidationError("Choose another name.")
        return value

    def clean(self):
        data = super().clean()
        if data.get("price", 0) < 0:
            self.add_error("price", "Enter a positive price.")
        return data
```

Use the form without HTTP or a builder:

```python
form = ProductForm({"name": "Notebook", "price": "12.50"})
if await form.is_valid():
    product = await form.save(commit=False)
else:
    print(form.errors)
```

Validation does not persist or mutate a supplied instance. `save(commit=False)`
applies cleaned values without inserting/updating. Use `await form.save()` for
persistence, or group operations inside `async with atomic()`.
Fields are copied per instance. `initial` and `prefix` follow Django conventions.
Read errors only after awaiting validation. Form fieldsets are configured on the
[builder](../../features/resources.md), not on Meta.
