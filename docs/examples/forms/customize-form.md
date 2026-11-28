# Customize a product form

Declare data constraints on the Resource's Pydantic schema and HTML options
on the form's control.

```python
from decimal import Decimal
from typing import Annotated

from obox import ModelResource, forms
from obox.views.generic import CreateView, ModelViewSet
from pydantic import BaseModel, Field

from .models import Product


class ProductInput(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=100)]
    price: Annotated[Decimal, Field(gt=0)]


class Products(ModelResource[Product]):
    model = Product
    write_schema = ProductInput


class ProductForm(forms.Form[ProductInput]):
    schema = ProductInput
    field_options = {
        "name": forms.Field(widget=forms.TextInput({"placeholder": "Name"})),
    }


class ProductCreate(CreateView[Product]):
    form_class = ProductForm


class ProductPages(ModelViewSet[Product]):
    resource = Products
    create_view = ProductCreate


obox.register(Products, views=ProductPages)
```

The create view runs the same write validation as REST. For optional
application logic, put the hook on `Products` or call a service from it.
