# Admin builders and product resources

ModelBuilder implements a small ModelAdmin subset for HTML administration:

```python
from obox.builder import ModelBuilder, register
from app.products.forms import ProductForm
from app.products.models import Product


@register(Product)
class ProductBuilder(ModelBuilder):
    form = ProductForm
    fields = ("name", "price")
    readonly_fields = ()
    list_display = ("id", "name", "price")
    search_fields = ("name",)
    ordering = ("name",)
    list_per_page = 100
```

Use fieldsets instead of fields to group controls. Customize get_form,
get_queryset, save_model/delete_model, actions and ModelAdmin-shaped permissions.
Register the model explicitly: `site.register(Product, ProductBuilder)` is the
non-decorator equivalent. No model inference or REST surface is involved.

The optional Resource product engine lives in `obox.contrib.resources`. It owns
rich record editing, policy-scoped queries, metadata, batch/upsert and related
product features. See [register a product resource](../examples/features/register-resource.md)
and [the protocol](../contrib-resource-api.md).
