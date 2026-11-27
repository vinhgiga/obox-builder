# Customize a model form

```python
from decimal import Decimal
from typing import Any, ClassVar

from obox import forms
from pydantic import BaseModel, field_validator, model_validator

from app.products.models import Product


class ProductInput(BaseModel):
    name: str
    price: Decimal

    @field_validator("name")
    @classmethod
    def reserved_name(cls, value: str) -> str:
        if value.lower() == "reserved":
            raise ValueError("Choose another name.")
        return value

    @model_validator(mode="after")
    def positive_price(self) -> "ProductInput":
        if self.price < 0:
            raise ValueError("Enter a positive price.")
        return self


class ProductForm(forms.ModelForm[ProductInput, Product]):
    schema = ProductInput

    class Meta:
        model = Product
        fields = ("name", "price")
        labels: ClassVar[dict[str, str]] = {"name": "Product name"}
        widgets: ClassVar[dict[str, Any]] = {"name": forms.TextInput(attrs={"placeholder": "Name"})}
        help_texts: ClassVar[dict[str, str]] = {"price": "Enter a positive price."}
```

Pydantic owns value and cross-field validation; `Meta` only configures which
model fields the form exposes and how they render. Override `post_clean()` when
validation needs the active database session. `ModelForm` already reports model
uniqueness and foreign-key errors there.

Use the form without HTTP or a view:

```python
async def create_product() -> None:
    form = ProductForm({"name": "Notebook", "price": "12.50"})
    if await form.is_valid():
        product = await form.save(commit=False)
    else:
        print(form.errors)
```

Validation does not persist or mutate a supplied instance. `save(commit=False)`
applies cleaned values without inserting/updating. Use `await form.save()` for
persistence, or group operations inside `async with atomic()` from `obox.db`.
Fields are copied per instance. `initial` and `prefix` follow Django conventions.
Read errors only after awaiting validation.

Form fieldsets and inlines are configured on the generic editing views, not on
`Meta`.

```python
from obox.views.generic import CreateView, UpdateView


class ProductCreate(CreateView):
    form_class = ProductForm
    fieldsets = (("Details", {"fields": ("name", "price")}),)


class ProductUpdate(UpdateView):
    form_class = ProductForm
    fieldsets = ProductCreate.fieldsets


obox.register(
    Product,
    create_view=ProductCreate,
    update_view=ProductUpdate,
)
```
