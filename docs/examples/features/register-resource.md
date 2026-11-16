# Register a contrib resource

This is the optional product resource editor, independent of ModelBuilder.
Enable `obox.contrib.resources` in INSTALLED_APPS. Use another UI prefix when
also mounting the standard builder, for example `RESOURCES = {'ui_prefix': '/workspace'}`.

```python
from obox.apps import AppConfig
from obox.contrib.resources import Resource, RBAC, FormOptions, ViewOptions, ApiOptions
from app.products.models import Product


class ProductResource(Resource):
    model = Product
    fields = ("name", "price")
    policy = RBAC("products.products")
    form_options = FormOptions
    view_options = ViewOptions
    api_options = ApiOptions


class ProductsConfig(AppConfig):
    name = "app.products"

    def ready(self):
        self.resources = (ProductResource,)
```

Declare resources during ready so application models have already loaded.
These options describe the contrib product protocol. They are not aliases for
core Form, generic View or DRF viewsets. Product permission names use
`app.resource.action`; admin permissions use Django-shaped `app.action_model`.
